import assert from 'node:assert/strict'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { after, beforeEach, test } from 'node:test'

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'


const frontendRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const vite = await createServer({
  appType: 'custom',
  configFile: false,
  logLevel: 'silent',
  resolve: { alias: { '@': resolve(frontendRoot, 'src') } },
  root: frontendRoot,
  server: { middlewareMode: true, ws: false },
})
after(async () => vite.close())

const values = new Map()
globalThis.localStorage = {
  getItem: key => values.get(key) ?? null,
  setItem: (key, value) => values.set(key, value),
  removeItem: key => values.delete(key),
}
beforeEach(() => values.clear())

const { useStrategyPool } = await vite.ssrLoadModule('/src/lib/useStrategyPool.ts')
const poolQueryKey = ['screener-strategy-pool']

function renderProbe(queryClient) {
  function Probe() {
    const state = useStrategyPool()
    return React.createElement('span', {
      'data-error': String(state.isError),
      'data-error-kind': String(state.errorKind),
      'data-ready': String(state.isReady),
      'data-saving': String(state.isSaving),
    }, state.pool.join(','))
  }

  return renderToStaticMarkup(
    React.createElement(
      QueryClientProvider,
      { client: queryClient },
      React.createElement(Probe),
    ),
  )
}

function createQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, retryOnMount: false },
    },
  })
}

test('服务端策略池尚未返回时向消费者暴露未就绪状态', () => {
  const markup = renderProbe(createQueryClient())
  assert.match(markup, /data-ready="false"/)
  assert.match(markup, /data-error="false"/)
  assert.match(markup, /data-saving="false"/)
})

test('服务端策略池加载失败时向消费者暴露错误且保持未就绪', async () => {
  const queryClient = createQueryClient()
  await queryClient.prefetchQuery({
    queryKey: poolQueryKey,
    queryFn: async () => { throw new Error('synthetic failure') },
  })

  const markup = renderProbe(queryClient)
  assert.match(markup, /data-ready="false"/)
  assert.match(markup, /data-error="true"/)
  assert.match(markup, /data-error-kind="load"/)
})

test('策略池保存期间阻止新消费者使用乐观缓存', () => {
  const queryClient = createQueryClient()
  queryClient.setQueryData(poolQueryKey, { strategy_ids: ['latest'] })
  queryClient.setQueryData(
    ['screener-strategy-pool-persistence'],
    { status: 'saving', desiredPool: ['latest'], errorMessage: null },
  )

  const markup = renderProbe(queryClient)
  assert.match(markup, /data-ready="false"/)
  assert.match(markup, /data-saving="true"/)
})

test('策略池保存失败时暴露保存错误且保持未就绪', () => {
  const queryClient = createQueryClient()
  queryClient.setQueryData(poolQueryKey, { strategy_ids: ['latest'] })
  queryClient.setQueryData(
    ['screener-strategy-pool-persistence'],
    { status: 'error', desiredPool: ['latest'], errorMessage: 'synthetic save failure' },
  )

  const markup = renderProbe(queryClient)
  assert.match(markup, /data-ready="false"/)
  assert.match(markup, /data-error="true"/)
  assert.match(markup, /data-error-kind="save"/)
})

test('刷新后从明确的 pending journal 恢复保存错误和重试目标', () => {
  values.set('strategy-pool-pending-v1', JSON.stringify({
    version: 1,
    status: 'pending',
    desiredPool: ['latest'],
  }))
  const queryClient = createQueryClient()
  queryClient.setQueryData(poolQueryKey, { strategy_ids: ['server-old'] })

  const markup = renderProbe(queryClient)
  assert.match(markup, /data-ready="false"/)
  assert.match(markup, /data-error="true"/)
  assert.match(markup, /data-error-kind="save"/)
})
