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
const {
  buildInlineTradeCreatePayload,
  buildInlineTradeDraft,
  buildTradeInsertionTargets,
} = await import(moduleUrl)


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


test('内联草稿复制来源交易的方向、数量和成交价', () => {
  const draft = buildInlineTradeDraft({
    id: 'source-trade',
    account_id: 'account-1',
    symbol: '600519.SH',
    trade_date: '2026-08-01',
    side: 'sell',
    quantity: 88.5,
    price: 1234.567,
  }, {
    tradeDate: '2026-08-01',
    insertBeforeTradeId: 'source-trade',
  })

  assert.deepEqual(draft, {
    sourceTradeId: 'source-trade',
    accountId: 'account-1',
    symbol: '600519.SH',
    tradeDate: '2026-08-01',
    side: 'sell',
    quantity: '88.5',
    price: '1234.567',
    insertBeforeTradeId: 'source-trade',
  })
})


test('内联草稿提交时省略费用和税费，由后端自动估算', () => {
  const payload = buildInlineTradeCreatePayload({
    sourceTradeId: 'source-trade',
    accountId: 'account-1',
    symbol: '600519.sh',
    tradeDate: '2026-08-01',
    side: 'buy',
    quantity: '100',
    price: '10.25',
    insertBeforeTradeId: 'source-trade',
  })

  assert.deepEqual(payload, {
    account_id: 'account-1',
    symbol: '600519.SH',
    trade_date: '2026-08-01',
    side: 'buy',
    quantity: 100,
    price: 10.25,
    insert_before_trade_id: 'source-trade',
  })
  assert.equal(Object.hasOwn(payload, 'fee'), false)
  assert.equal(Object.hasOwn(payload, 'tax'), false)
})


test('内联草稿拒绝空值、非数字和非法数量价格', () => {
  const valid = {
    sourceTradeId: 'source-trade',
    accountId: 'account-1',
    symbol: '600519.SH',
    tradeDate: '2026-08-01',
    side: 'buy',
    quantity: '100',
    price: '10',
  }

  assert.equal(buildInlineTradeCreatePayload({ ...valid, quantity: '' }), null)
  assert.equal(buildInlineTradeCreatePayload({ ...valid, quantity: 'not-a-number' }), null)
  assert.equal(buildInlineTradeCreatePayload({ ...valid, quantity: '0' }), null)
  assert.equal(buildInlineTradeCreatePayload({ ...valid, price: '' }), null)
  assert.equal(buildInlineTradeCreatePayload({ ...valid, price: '-1' }), null)
})
