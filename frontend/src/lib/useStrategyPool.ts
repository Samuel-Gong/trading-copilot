import { useState, useCallback, useEffect, useRef } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { storage } from '@/lib/storage'
import {
  canHydrateStrategyPool,
  createPendingStrategyPoolSave,
  createStrategyPoolSaveQueue,
  normalizeStrategyPool,
  parsePendingStrategyPoolSave,
  resolveRefetchedStrategyPool,
  resolveStrategyPool,
  strategyPoolsEqual,
  strategyPoolQueryPolicy,
} from '@/lib/strategyPoolPersistence'

type StrategyPoolPersistenceState =
  | { status: 'idle'; desiredPool: null; errorMessage: null }
  | { status: 'saving'; desiredPool: string[]; errorMessage: null }
  | { status: 'error'; desiredPool: string[]; errorMessage: string }

const IDLE_PERSISTENCE_STATE: StrategyPoolPersistenceState = {
  status: 'idle',
  desiredPool: null,
  errorMessage: null,
}

function restorePersistenceState(): StrategyPoolPersistenceState {
  const pendingPool = parsePendingStrategyPoolSave(
    storage.strategyPoolPending.getOptional(),
  )
  if (pendingPool === null) return IDLE_PERSISTENCE_STATE
  return {
    status: 'error',
    desiredPool: pendingPool,
    errorMessage: '上次策略池保存未完成',
  }
}

const strategyPoolSaveQueue = createStrategyPoolSaveQueue(async pool => {
  const saved = await api.updateScreenerStrategyPool(pool)
  return saved.strategy_ids
})

export function useStrategyPool() {
  const localPoolRef = useRef<unknown>(storage.strategyPool.getOptional())
  const poolRef = useRef<string[]>([])
  const [pool, setPool] = useState<string[]>([])
  const [isHydrated, setIsHydrated] = useState(false)
  const hydratedRef = useRef(false)
  const queryClient = useQueryClient()

  const persistence = useQuery<StrategyPoolPersistenceState>({
    queryKey: QK.screenerStrategyPoolPersistence,
    queryFn: async () => IDLE_PERSISTENCE_STATE,
    initialData: restorePersistenceState,
    enabled: false,
    staleTime: Infinity,
    gcTime: strategyPoolQueryPolicy.persistenceGcTime,
  })

  const serverPool = useQuery({
    queryKey: QK.screenerStrategyPool,
    queryFn: api.screenerStrategyPool,
    enabled: persistence.data.status === 'idle',
    staleTime: strategyPoolQueryPolicy.serverStaleTime,
    refetchOnMount: strategyPoolQueryPolicy.serverRefetchOnMount,
  })

  const persistToServer = useCallback((next: string[]) => {
    const desiredPool = [...next]
    storage.strategyPoolPending.set(createPendingStrategyPoolSave(desiredPool))
    void queryClient.cancelQueries({ queryKey: QK.screenerStrategyPool })
    queryClient.setQueryData<StrategyPoolPersistenceState>(
      QK.screenerStrategyPoolPersistence,
      { status: 'saving', desiredPool, errorMessage: null },
    )
    queryClient.setQueryData(QK.screenerStrategyPool, { strategy_ids: desiredPool })
    void strategyPoolSaveQueue.enqueue(desiredPool, {
      onSaved: savedPool => {
        storage.strategyPool.set(savedPool)
        storage.strategyPoolPending.clear()
        queryClient.setQueryData(QK.screenerStrategyPool, { strategy_ids: savedPool })
        queryClient.setQueryData<StrategyPoolPersistenceState>(
          QK.screenerStrategyPoolPersistence,
          IDLE_PERSISTENCE_STATE,
        )
      },
      onError: (failedPool, error) => {
        storage.strategyPoolPending.set(createPendingStrategyPoolSave(failedPool))
        queryClient.setQueryData<StrategyPoolPersistenceState>(
          QK.screenerStrategyPoolPersistence,
          {
            status: 'error',
            desiredPool: failedPool,
            errorMessage: error instanceof Error ? error.message : '策略池保存失败',
          },
        )
      },
    })
  }, [queryClient])

  useEffect(() => {
    if (!canHydrateStrategyPool(
      serverPool.isSuccess,
      serverPool.isFetchedAfterMount,
      persistence.data.status === 'idle',
    )) return
    if (!serverPool.data) return
    if (!hydratedRef.current) {
      hydratedRef.current = true
      const resolved = resolveStrategyPool(
        localPoolRef.current,
        serverPool.data.strategy_ids,
      )
      poolRef.current = resolved.pool
      localPoolRef.current = resolved.pool
      storage.strategyPool.set(resolved.pool)
      setPool(resolved.pool)
      if (resolved.persistToServer) persistToServer(resolved.pool)
      setIsHydrated(true)
      return
    }

    const refreshed = resolveRefetchedStrategyPool(
      poolRef.current,
      serverPool.data.strategy_ids,
    )
    if (refreshed === null) return
    poolRef.current = refreshed
    localPoolRef.current = refreshed
    storage.strategyPool.set(refreshed)
    setPool(refreshed)
  }, [
    persistToServer,
    persistence.data.status,
    serverPool.data,
    serverPool.isFetchedAfterMount,
    serverPool.isSuccess,
  ])

  const commit = useCallback((update: (previous: string[]) => string[]) => {
    if (!hydratedRef.current) return
    const next = normalizeStrategyPool(update(poolRef.current))
    if (strategyPoolsEqual(poolRef.current, next)) return
    poolRef.current = next
    localPoolRef.current = next
    storage.strategyPool.set(next)
    setPool(next)
    persistToServer(next)
  }, [persistToServer])

  const addToPool = useCallback((id: string) => {
    commit(prev => prev.includes(id) ? prev : [...prev, id])
  }, [commit])

  const removeFromPool = useCallback((id: string) => {
    commit(prev => prev.filter(x => x !== id))
  }, [commit])

  const reorderPool = useCallback((newOrder: string[]) => {
    commit(() => newOrder)
  }, [commit])

  const isInPool = useCallback((id: string) => pool.includes(id), [pool])

  const retry = useCallback(() => {
    if (persistence.data.status === 'error') {
      persistToServer(persistence.data.desiredPool)
      return
    }
    void serverPool.refetch()
  }, [persistToServer, persistence.data, serverPool])

  const errorKind = persistence.data.status === 'error'
    ? 'save'
    : !isHydrated && serverPool.isError ? 'load' : null
  const isSaving = persistence.data.status === 'saving'

  return {
    pool,
    isReady: isHydrated && persistence.data.status === 'idle',
    isSaving,
    isError: errorKind !== null,
    errorKind,
    errorMessage: persistence.data.errorMessage,
    retry,
    addToPool,
    removeFromPool,
    reorderPool,
    isInPool,
  }
}
