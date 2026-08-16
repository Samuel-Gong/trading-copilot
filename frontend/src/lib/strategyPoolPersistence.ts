export interface ResolvedStrategyPool {
  pool: string[]
  persistToServer: boolean
}

export interface StrategyPoolSaveCallbacks {
  onSaved: (pool: string[]) => void
  onError: (pool: string[], error: unknown) => void
}

export interface StrategyPoolSaveQueue {
  enqueue: (pool: string[], callbacks: StrategyPoolSaveCallbacks) => Promise<void>
}

export interface PendingStrategyPoolSave {
  version: 1
  status: 'pending'
  desiredPool: string[]
}

export function normalizeStrategyPool(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  const seen = new Set<string>()
  const normalized: string[] = []
  for (const raw of value) {
    if (typeof raw !== 'string') continue
    const strategyId = raw.trim()
    if (!strategyId || seen.has(strategyId)) continue
    seen.add(strategyId)
    normalized.push(strategyId)
  }
  return normalized
}

export function strategyPoolsEqual(left: string[], right: string[]): boolean {
  return left.length === right.length && left.every((id, index) => id === right[index])
}

export function createPendingStrategyPoolSave(pool: string[]): PendingStrategyPoolSave {
  return {
    version: 1,
    status: 'pending',
    desiredPool: normalizeStrategyPool(pool),
  }
}

/** 普通旧版策略池数组不具备待重试语义，只有显式 journal 才能恢复。 */
export function parsePendingStrategyPoolSave(value: unknown): string[] | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const journal = value as Partial<PendingStrategyPoolSave>
  if (
    journal.version !== 1
    || journal.status !== 'pending'
    || !Array.isArray(journal.desiredPool)
  ) return null
  return normalizeStrategyPool(journal.desiredPool)
}

export function canHydrateStrategyPool(
  isSuccess: boolean,
  isFetchedAfterMount: boolean,
  persistenceIdle: boolean,
): boolean {
  return isSuccess && isFetchedAfterMount && persistenceIdle
}

export function canPruneUnavailableStrategyPool(
  isSuccess: boolean,
  isFetchedAfterMount: boolean,
  isFetching: boolean,
  hasLoadErrors: boolean,
  availableStrategyCount: number,
): boolean {
  return isSuccess
    && isFetchedAfterMount
    && !isFetching
    && !hasLoadErrors
    && availableStrategyCount > 0
}

export function pruneUnavailableStrategyPool(
  pool: unknown,
  availableIds: Iterable<string>,
  canPrune: boolean,
): string[] {
  const normalized = normalizeStrategyPool(pool)
  if (!canPrune) return normalized
  const available = new Set(availableIds)
  return normalized.filter(id => available.has(id))
}

/** hydration 后只接受服务端明确保存过的数组; null 不覆盖当前有效状态。 */
export function resolveRefetchedStrategyPool(
  currentPool: unknown,
  serverValue: unknown,
): string[] | null {
  if (!Array.isArray(serverValue)) return null
  const current = normalizeStrategyPool(currentPool)
  const refreshed = normalizeStrategyPool(serverValue)
  return strategyPoolsEqual(current, refreshed) ? null : refreshed
}

export const strategyPoolQueryPolicy = {
  // 策略池以服务端为准；即使命中新鲜缓存，每个新消费者也要完成一次 GET。
  serverStaleTime: 0,
  serverRefetchOnMount: 'always',
  // saving/error 中保存着尚未落盘的目标，不能在页面离开后被默认 GC 丢弃。
  persistenceGcTime: Infinity,
} as const

/**
 * 串行保存策略池，但只允许最新入队请求的结果更新前端共享状态。
 * 中间响应仍会按顺序落到服务端，随后由最新请求覆盖，不得把共享缓存回退。
 */
export function createStrategyPoolSaveQueue(
  save: (pool: string[]) => Promise<string[]>,
): StrategyPoolSaveQueue {
  let tail = Promise.resolve()
  let latestRevision = 0

  return {
    enqueue(pool, callbacks) {
      const revision = ++latestRevision
      const snapshot = [...pool]
      const task = tail.then(async () => {
        try {
          const saved = normalizeStrategyPool(await save(snapshot))
          if (revision === latestRevision) callbacks.onSaved(saved)
        } catch (error) {
          if (revision === latestRevision) callbacks.onError(snapshot, error)
        }
      })
      tail = task.catch(() => undefined)
      return task
    },
  }
}

/**
 * 只有服务端明确返回 null（尚未迁移）时，旧 localStorage 才是一次性迁移源。
 * 服务端一旦保存过数组（包括显式空数组），此后始终以服务端为准，
 * 避免其他浏览器或旧来源用陈旧本地值覆盖新选择。
 */
export function resolveStrategyPool(localValue: unknown, serverValue: unknown): ResolvedStrategyPool {
  const serverPool = normalizeStrategyPool(serverValue)
  if (serverValue !== null || !Array.isArray(localValue)) {
    return { pool: serverPool, persistToServer: false }
  }
  const localPool = normalizeStrategyPool(localValue)
  return {
    pool: localPool,
    persistToServer: true,
  }
}
