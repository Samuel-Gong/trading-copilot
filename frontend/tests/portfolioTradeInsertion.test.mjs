import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import ts from 'typescript'


const source = await readFile(
  new URL('../src/pages/portfolio/tradeInsertion.ts', import.meta.url),
  'utf8',
)
const { outputText } = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2022,
  },
})
const moduleUrl = `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
const { buildTradeInsertionTargets } = await import(moduleUrl)


test('空明细使用估值日创建第一条交易', () => {
  assert.deepEqual(buildTradeInsertionTargets([], '2026-08-01'), [
    { tradeDate: '2026-08-01' },
  ])
})


test('每行之后的插入位置映射到该行记录及其交易日', () => {
  const targets = buildTradeInsertionTargets([
    { id: 'latest', trade_date: '2026-08-01' },
    { id: 'same-day-earlier', trade_date: '2026-08-01' },
    { id: 'older-day', trade_date: '2026-07-31' },
  ], '2026-08-01')

  assert.deepEqual(targets, [
    { tradeDate: '2026-08-01', insertBeforeTradeId: 'latest' },
    { tradeDate: '2026-08-01', insertBeforeTradeId: 'same-day-earlier' },
    { tradeDate: '2026-07-31', insertBeforeTradeId: 'older-day' },
  ])
})
