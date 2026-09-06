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
const { outputText } = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2022,
  },
})
const moduleUrl = `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
const {
  requiresTransientBatchRows,
  resultsForSelectedDate,
  shouldRefreshTransientBatchForColumns,
  transientBatchColumnRefreshKey,
  transientBatchColumnRetryParams,
  removeTransientBatchResults,
  updateTransientBatchResult,
} = await import(moduleUrl)


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
  assert.match(pageSource, /const screenerRunEpochRef = useRef\(0\)/)
  assert.match(pageSource, /if \(vars\.epoch !== screenerRunEpochRef\.current\) return/)
  assert.match(pageSource, /screenerRunEpochRef\.current \+= 1[\s\S]*?setTransientBatchResults\(null\)[\s\S]*?setAssetType\(nextAssetType\)/)
})
