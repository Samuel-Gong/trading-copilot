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
const { requiresTransientBatchRows, resultsForSelectedDate } = await import(moduleUrl)


test('仅在历史日期早于已保存快照时请求批量明细', () => {
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
