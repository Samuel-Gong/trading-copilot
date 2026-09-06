import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import ts from 'typescript'


const source = await readFile(
  new URL('../src/lib/screenerBatchResults.ts', import.meta.url),
  'utf8',
)
const pageSource = await readFile(
  new URL('../src/pages/Screener.tsx', import.meta.url),
  'utf8',
)
const settingsSource = await readFile(
  new URL('../src/components/screener/StrategySettingsDialog.tsx', import.meta.url),
  'utf8',
)
const requestCoordinatorSource = await readFile(
  new URL('../src/lib/screenerRequestCoordinator.ts', import.meta.url),
  'utf8',
)
const { outputText } = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2022,
  },
})
const moduleUrl = `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
const { outputText: requestCoordinatorOutput } = ts.transpileModule(requestCoordinatorSource, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2022,
  },
})
const requestCoordinatorUrl = `data:text/javascript;base64,${Buffer.from(requestCoordinatorOutput).toString('base64')}`
const {
  requiresTransientBatchRows,
  resultsForSelectedDate,
  shouldRefreshTransientBatchForColumns,
  transientBatchColumnRefreshKey,
  transientBatchColumnRetryParams,
  mergeTransientBatchResults,
  removeTransientBatchResults,
  updateTransientBatchResult,
} = await import(moduleUrl)
const {
  bindScreenerRequestContext,
  createScreenerRequestContext,
  createScreenerRequestCoordinator,
  isCurrentScreenerRequest,
  mergeScreenerRunAllStrategyIds,
  shouldPreserveScreenerConfigReruns,
} = await import(requestCoordinatorUrl)


test('仅在历史日期早于最新数据日期时请求批量明细', () => {
  assert.equal(requiresTransientBatchRows('2026-09-03', '2026-09-04'), true)
  assert.equal(requiresTransientBatchRows('2026-09-04', '2026-09-04'), false)
  assert.equal(requiresTransientBatchRows('2026-09-05', '2026-09-04'), false)
  assert.equal(requiresTransientBatchRows('2026-09-03', null), false)
})


test('历史批量明细优先于较新的共享快照', () => {
  const cached = {
    as_of: '2026-09-04',
    asset_type: 'stock',
    results: { alpha: { as_of: '2026-09-04', total: 1, rows: [{ symbol: '000001.SZ' }] } },
  }
  const transient = {
    as_of: '2026-09-03',
    asset_type: 'stock',
    results: { alpha: { as_of: '2026-09-03', total: 1, rows: [{ symbol: '000003.SZ' }] } },
  }

  assert.equal(resultsForSelectedDate('2026-09-03', null, cached), null)
  assert.deepEqual(resultsForSelectedDate('2026-09-03', transient, cached, 'stock'), transient.results)
  assert.deepEqual(resultsForSelectedDate('2026-09-04', transient, cached, 'stock'), cached.results)
  assert.equal(resultsForSelectedDate('2026-09-03', transient, cached, 'etf'), null)
})


test('历史单策略重跑同步替换临时批量明细及扩展列', () => {
  const transient = {
    as_of: '2026-09-03',
    asset_type: 'stock',
    results: { alpha: { as_of: '2026-09-03', total: 1, rows: [{ symbol: '000001.SZ' }] } },
  }

  const updated = updateTransientBatchResult(transient, 'alpha', {
    as_of: '2026-09-03',
    total: 1,
    rows: [{ symbol: '000002.SZ', synthetic__value: 7 }],
  })

  assert.deepEqual(resultsForSelectedDate('2026-09-03', updated, undefined, 'stock'), {
    alpha: {
      as_of: '2026-09-03',
      total: 1,
      rows: [{ symbol: '000002.SZ', synthetic__value: 7 }],
    },
  })
})


test('配置变更会移除目标策略及叠加策略的历史临时结果', () => {
  const transient = {
    as_of: '2026-09-03',
    asset_type: 'stock',
    results: {
      alpha: { as_of: '2026-09-03', total: 1, rows: [{ symbol: '000001.SZ' }] },
      blend: { as_of: '2026-09-03', total: 1, rows: [{ symbol: '000002.SZ' }] },
      beta: { as_of: '2026-09-03', total: 1, rows: [{ symbol: '000003.SZ' }] },
    },
  }

  assert.deepEqual(removeTransientBatchResults(transient, ['alpha', 'blend']), {
    ...transient,
    results: { beta: transient.results.beta },
  })
})


test('历史策略局部重跑保留未受影响的临时明细', () => {
  const transient = {
    as_of: '2026-09-03',
    asset_type: 'stock',
    results: {
      alpha: { as_of: '2026-09-03', total: 1, rows: [{ symbol: '000001.SZ' }] },
      beta: { as_of: '2026-09-03', total: 1, rows: [{ symbol: '000002.SZ' }] },
    },
  }
  const afterConfigSave = removeTransientBatchResults(transient, ['alpha'])
  const rerunAlpha = {
    as_of: '2026-09-03',
    asset_type: 'stock',
    results: {
      alpha: { as_of: '2026-09-03', total: 1, rows: [{ symbol: '000003.SZ' }] },
    },
  }

  assert.deepEqual(
    mergeTransientBatchResults(afterConfigSave, rerunAlpha, ['alpha']),
    {
      ...rerunAlpha,
      results: { alpha: rerunAlpha.results.alpha, beta: transient.results.beta },
    },
  )
})


test('失效后的批量重跑会等待旧请求结束，并拒绝旧上下文', () => {
  const coordinator = createScreenerRequestCoordinator()
  const context = createScreenerRequestContext({ asOf: '2026-09-03', assetType: 'stock' })
  const started = []
  const start = request => started.push(request)

  coordinator.request({ id: 'old-date', context: context.current() }, start)
  assert.deepEqual(started, [{
    id: 'old-date',
    context: { asOf: '2026-09-03', assetType: 'stock', version: 0 },
    epoch: 0,
  }])

  context.invalidate({ asOf: '2026-09-04' })
  coordinator.invalidate()
  coordinator.request({ id: 'new-config', context: context.current() }, start)

  assert.equal(coordinator.isCurrent(started[0].epoch), false)
  assert.equal(context.matches(started[0].context), false)
  assert.equal(started.length, 1)

  coordinator.settle(start)
  assert.deepEqual(started, [
    {
      id: 'old-date',
      context: { asOf: '2026-09-03', assetType: 'stock', version: 0 },
      epoch: 0,
    },
    {
      id: 'new-config',
      context: { asOf: '2026-09-04', assetType: 'stock', version: 1 },
      epoch: 1,
    },
  ])
  assert.equal(coordinator.isCurrent(started[1].epoch), true)
  assert.equal(context.matches(started[1].context), true)
})


test('连续保存时排队批跑会累积所有失效策略', () => {
  const coordinator = createScreenerRequestCoordinator()
  const context = createScreenerRequestContext({ asOf: '2026-09-03', assetType: 'stock' })
  const started = []
  const start = request => started.push(request)
  let configRerunStrategyIds = []

  coordinator.request({ id: 'old', context: context.current() }, start)

  context.invalidate()
  coordinator.invalidate()
  configRerunStrategyIds = mergeScreenerRunAllStrategyIds(configRerunStrategyIds, ['alpha'])
  coordinator.request({ strategyIds: configRerunStrategyIds, context: context.current() }, start)

  context.invalidate()
  coordinator.invalidate()
  configRerunStrategyIds = mergeScreenerRunAllStrategyIds(configRerunStrategyIds, ['beta'])
  coordinator.request({ strategyIds: configRerunStrategyIds, context: context.current() }, start)

  coordinator.settle(start)
  assert.deepEqual(started[1], {
    strategyIds: ['alpha', 'beta'],
    context: { asOf: '2026-09-03', assetType: 'stock', version: 2 },
    epoch: 2,
  })
})


test('同一上下文的视图切换会重绑配置重跑并刷新摘要', () => {
  const coordinator = createScreenerRequestCoordinator()
  const context = createScreenerRequestContext({ asOf: '2026-09-03', assetType: 'stock' })
  const started = []
  const start = request => started.push(request)
  const configRerunStrategyIds = ['alpha']

  coordinator.request({ vars: { strategyIds: ['old'], context: context.current() } }, start)

  context.invalidate()
  coordinator.invalidate()
  coordinator.request({ vars: { strategyIds: configRerunStrategyIds, context: context.current() } }, start)

  assert.equal(shouldPreserveScreenerConfigReruns(context.current()), true)
  context.invalidate()
  coordinator.invalidate()
  coordinator.request({ vars: { strategyIds: configRerunStrategyIds, context: context.current() } }, start)

  coordinator.settle(start)
  assert.deepEqual(started[1], {
    vars: {
      strategyIds: ['alpha'],
      context: { asOf: '2026-09-03', assetType: 'stock', version: 2 },
    },
    epoch: 2,
  })

  const summaryInvalidations = []
  if (isCurrentScreenerRequest(
    { ...started[1].vars, epoch: started[1].epoch },
    context.current(),
    coordinator.currentEpoch(),
  )) {
    summaryInvalidations.push('screener-cached')
  }
  assert.deepEqual(summaryInvalidations, ['screener-cached'])
})


test('日期、资产或策略池变化会清空配置重跑队列', () => {
  const context = { asOf: '2026-09-03', assetType: 'stock' }

  assert.equal(shouldPreserveScreenerConfigReruns(context), true)
  assert.equal(shouldPreserveScreenerConfigReruns(context, { asOf: '2026-09-04' }), false)
  assert.equal(shouldPreserveScreenerConfigReruns(context, { assetType: 'etf' }), false)
  assert.equal(shouldPreserveScreenerConfigReruns(context, {}, false), false)
})


test('旧渲染闭包不能以新代际发起旧日期或旧资产请求', () => {
  const context = createScreenerRequestContext({ asOf: '2026-09-04', assetType: 'stock' })

  assert.equal(bindScreenerRequestContext({ date: '2026-09-03' }, context.current()), null)
  assert.equal(bindScreenerRequestContext({ assetType: 'etf' }, context.current()), null)
  assert.deepEqual(bindScreenerRequestContext({ strategyIds: ['alpha'] }, context.current()), {
    strategyIds: ['alpha'],
    date: '2026-09-04',
    assetType: 'stock',
    context: { asOf: '2026-09-04', assetType: 'stock', version: 0 },
  })
})


test('历史临时明细在扩展列配置变化后需要重新读取', () => {
  const transient = {
    as_of: '2026-09-03',
    asset_type: 'stock',
    ext_columns: 'synthetic__old',
    results: { alpha: { as_of: '2026-09-03', total: 0, rows: [] } },
  }

  assert.equal(shouldRefreshTransientBatchForColumns(transient, '2026-09-03', 'synthetic__new', 'stock'), true)
  assert.equal(shouldRefreshTransientBatchForColumns(transient, '2026-09-03', 'synthetic__old', 'stock'), false)
  assert.equal(shouldRefreshTransientBatchForColumns(transient, '2026-09-03', 'synthetic__old', 'etf'), true)
  assert.equal(shouldRefreshTransientBatchForColumns(transient, '2026-09-04', 'synthetic__new'), false)
  assert.equal(transientBatchColumnRefreshKey(transient, '2026-09-03', 'synthetic__new'), '2026-09-03\u0000stock\u0000synthetic__new')
  assert.equal(transientBatchColumnRefreshKey(transient, '2026-09-03', 'synthetic__old'), null)

  assert.deepEqual(transientBatchColumnRetryParams(transient, '2026-09-03', 'synthetic__new'), {
    date: '2026-09-03',
    strategyIds: ['alpha'],
    extColumns: 'synthetic__new',
  })
  assert.equal(transientBatchColumnRetryParams(transient, '2026-09-03', 'synthetic__old'), null)
})


test('历史扩展列刷新失败显示直接重试入口', () => {
  assert.match(pageSource, /runAll\.isError/)
  assert.match(pageSource, /onClick=\{\(\) => requestRunAll\(transientColumnRetryParams\)\}/)
  assert.match(pageSource, />\s*重试\s*<\/button>/)
})


test('重置策略配置会通知页面清理历史批量明细', () => {
  assert.match(settingsSource, /const reset = await api\.strategyResetConfig\(strategyId\)[\s\S]*?onSaved\?\.\(d\.display_limit \?\? null, reset\.invalidated_strategy_ids\)/)
  assert.match(settingsSource, /invalidated_strategy_ids/)
  assert.match(pageSource, /onSaved=\{\(limit, invalidatedStrategyIds\) => \{[\s\S]*?removeTransientBatchResults/)
})


test('切换资产类型会废弃旧请求和历史批量结果', () => {
  assert.match(pageSource, /createScreenerRequestCoordinator/)
  assert.match(pageSource, /invalidateScreenerRequests\(\)[\s\S]*?setTransientBatchResults\(null\)[\s\S]*?setAssetType\(nextAssetType\)/)
})
