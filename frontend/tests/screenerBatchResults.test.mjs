import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import ts from 'typescript'


const source = await readFile(
  new URL('../src/lib/screenerBatchResults.ts', import.meta.url),
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
    results: { alpha: { as_of: '2026-09-04', total: 1, rows: [{ symbol: '000001.SZ' }] } },
  }
  const transient = {
    as_of: '2026-09-03',
    results: { alpha: { as_of: '2026-09-03', total: 1, rows: [{ symbol: '000003.SZ' }] } },
  }

  assert.equal(resultsForSelectedDate('2026-09-03', null, cached), null)
  assert.deepEqual(resultsForSelectedDate('2026-09-03', transient, cached), transient.results)
  assert.deepEqual(resultsForSelectedDate('2026-09-04', transient, cached), cached.results)
})


test('历史单策略重跑同步替换临时批量明细及扩展列', () => {
  const transient = {
    as_of: '2026-09-03',
    results: { alpha: { as_of: '2026-09-03', total: 1, rows: [{ symbol: '000001.SZ' }] } },
  }

  const updated = updateTransientBatchResult(transient, 'alpha', {
    as_of: '2026-09-03',
    total: 1,
    rows: [{ symbol: '000002.SZ', synthetic__value: 7 }],
  })

  assert.deepEqual(resultsForSelectedDate('2026-09-03', updated, undefined), {
    alpha: {
      as_of: '2026-09-03',
      total: 1,
      rows: [{ symbol: '000002.SZ', synthetic__value: 7 }],
    },
  })
})


test('历史临时明细在扩展列配置变化后需要重新读取', () => {
  const transient = {
    as_of: '2026-09-03',
    ext_columns: 'synthetic__old',
    results: { alpha: { as_of: '2026-09-03', total: 0, rows: [] } },
  }

  assert.equal(shouldRefreshTransientBatchForColumns(transient, '2026-09-03', 'synthetic__new'), true)
  assert.equal(shouldRefreshTransientBatchForColumns(transient, '2026-09-03', 'synthetic__old'), false)
  assert.equal(shouldRefreshTransientBatchForColumns(transient, '2026-09-04', 'synthetic__new'), false)
  assert.equal(transientBatchColumnRefreshKey(transient, '2026-09-03', 'synthetic__new'), '2026-09-03\u0000synthetic__new')
  assert.equal(transientBatchColumnRefreshKey(transient, '2026-09-03', 'synthetic__old'), null)
})
