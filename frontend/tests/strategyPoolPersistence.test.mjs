import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import ts from 'typescript'


const source = await readFile(
  new URL('../src/lib/strategyPoolPersistence.ts', import.meta.url),
  'utf8',
)
const { outputText } = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2022,
  },
})
const moduleUrl = `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
const {
  canPruneUnavailableStrategyPool,
  canHydrateStrategyPool,
  createPendingStrategyPoolSave,
  createStrategyPoolSaveQueue,
  parsePendingStrategyPoolSave,
  pruneUnavailableStrategyPool,
  resolveRefetchedStrategyPool,
  resolveStrategyPool,
  strategyPoolQueryPolicy,
} = await import(moduleUrl)


function deferred() {
  let resolve
  const promise = new Promise(resolvePromise => {
    resolve = resolvePromise
  })
  return { promise, resolve }
}


test('新部署来源没有本地缓存时从服务端恢复策略池', () => {
  assert.deepEqual(
    resolveStrategyPool(undefined, ['builtin_a', 'custom_b']),
    {
      pool: ['builtin_a', 'custom_b'],
      persistToServer: false,
    },
  )
})


test('服务端尚未设置时迁移旧版本本地选择并保持顺序', () => {
  assert.deepEqual(
    resolveStrategyPool(['custom_b', 'builtin_a'], null),
    {
      pool: ['custom_b', 'builtin_a'],
      persistToServer: true,
    },
  )
})


test('服务端尚未设置时把旧版本显式空策略池也迁移到服务端', () => {
  assert.deepEqual(
    resolveStrategyPool([], null),
    {
      pool: [],
      persistToServer: true,
    },
  )
})


test('服务端已有策略池（包括空数组）时忽略陈旧的本地缓存', () => {
  assert.deepEqual(
    resolveStrategyPool(['stale_local'], ['server_a', 'server_b']),
    {
      pool: ['server_a', 'server_b'],
      persistToServer: false,
    },
  )
  assert.deepEqual(
    resolveStrategyPool(['stale_local'], []),
    {
      pool: [],
      persistToServer: false,
    },
  )
})


test('恢复时忽略非法值并对策略 ID 去重', () => {
  assert.deepEqual(
    resolveStrategyPool(undefined, ['builtin_a', '', 'builtin_a', 1]),
    {
      pool: ['builtin_a'],
      persistToServer: false,
    },
  )
})


test('本地缓存损坏时不覆盖服务端策略池', () => {
  assert.deepEqual(
    resolveStrategyPool({ strategy_ids: [] }, ['builtin_a']),
    {
      pool: ['builtin_a'],
      persistToServer: false,
    },
  )
})


test('挂载时的陈旧缓存不能在后台 GET 完成前触发 hydration', () => {
  assert.equal(canHydrateStrategyPool(true, false, true), false)
  assert.equal(canHydrateStrategyPool(true, true, true), true)
  assert.equal(canHydrateStrategyPool(true, true, false), false)
})


test('只在策略列表完整成功后剔除服务端策略池中的失效 ID', () => {
  const savedPool = ['builtin_a', 'removed', 'etf_only']
  const availableIds = ['builtin_a', 'etf_only']

  assert.deepEqual(
    pruneUnavailableStrategyPool(savedPool, availableIds, true),
    ['builtin_a', 'etf_only'],
  )
  assert.deepEqual(
    pruneUnavailableStrategyPool(savedPool, availableIds, false),
    savedPool,
  )
  assert.equal(canPruneUnavailableStrategyPool(true, true, false, false, 2), true)
  assert.equal(canPruneUnavailableStrategyPool(false, true, false, false, 2), false)
  assert.equal(canPruneUnavailableStrategyPool(true, false, false, false, 2), false)
  assert.equal(canPruneUnavailableStrategyPool(true, true, true, false, 2), false)
  assert.equal(canPruneUnavailableStrategyPool(true, true, false, true, 2), false)
  assert.equal(canPruneUnavailableStrategyPool(true, true, false, false, 0), false)
})


test('hydration 后的成功 GET 同步服务端最新策略池', () => {
  assert.deepEqual(
    resolveRefetchedStrategyPool(['old'], ['latest', 'second']),
    ['latest', 'second'],
  )
  assert.equal(resolveRefetchedStrategyPool(['latest'], ['latest']), null)
  assert.equal(resolveRefetchedStrategyPool(['local'], null), null)
})


test('策略池每次挂载都刷新且保存错误状态不会被 Query GC', () => {
  assert.equal(strategyPoolQueryPolicy.serverStaleTime, 0)
  assert.equal(strategyPoolQueryPolicy.serverRefetchOnMount, 'always')
  assert.equal(strategyPoolQueryPolicy.persistenceGcTime, Infinity)
})


test('只从明确的 pending journal 恢复待重试策略池', () => {
  const journal = createPendingStrategyPoolSave([' first ', '', 'first', 'second'])
  assert.deepEqual(journal, {
    version: 1,
    status: 'pending',
    desiredPool: ['first', 'second'],
  })
  assert.deepEqual(parsePendingStrategyPoolSave(journal), ['first', 'second'])
  assert.equal(parsePendingStrategyPoolSave(['stale-local-value']), null)
  assert.equal(parsePendingStrategyPoolSave({ version: 1, desiredPool: ['missing-marker'] }), null)
})


test('连续保存时只有最新响应可以更新共享状态', async () => {
  const requests = []
  const savedPools = []
  const queue = createStrategyPoolSaveQueue(pool => {
    const request = deferred()
    requests.push({ pool, request })
    return request.promise
  })

  const first = queue.enqueue(['first'], {
    onError: assert.fail,
    onSaved: pool => savedPools.push(pool),
  })
  const second = queue.enqueue(['latest'], {
    onError: assert.fail,
    onSaved: pool => savedPools.push(pool),
  })

  await Promise.resolve()
  assert.deepEqual(requests.map(item => item.pool), [['first']])
  requests[0].request.resolve(['first'])
  await first
  await Promise.resolve()
  assert.deepEqual(savedPools, [])
  assert.deepEqual(requests.map(item => item.pool), [['first'], ['latest']])

  requests[1].request.resolve(['latest'])
  await second
  assert.deepEqual(savedPools, [['latest']])
})


test('最后一次保存失败时暴露失败值并允许后续重试', async () => {
  let attempt = 0
  const failures = []
  const savedPools = []
  const queue = createStrategyPoolSaveQueue(async pool => {
    attempt += 1
    if (attempt === 1) throw new Error('synthetic save failure')
    return pool
  })

  await queue.enqueue(['latest'], {
    onError: (pool, error) => failures.push({ pool, message: error.message }),
    onSaved: pool => savedPools.push(pool),
  })
  assert.deepEqual(failures, [{ pool: ['latest'], message: 'synthetic save failure' }])
  assert.deepEqual(savedPools, [])

  await queue.enqueue(['latest'], {
    onError: assert.fail,
    onSaved: pool => savedPools.push(pool),
  })
  assert.deepEqual(savedPools, [['latest']])
})
