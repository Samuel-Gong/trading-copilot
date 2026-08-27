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
  filterStockTradeGroups,
  persistInlineTradeDraft,
  reduceLedgerInlineTradeState,
} = await import(moduleUrl)


const sourceTrade = {
  id: 'source-trade',
  account_id: 'account-1',
  symbol: '600519.SH',
  trade_date: '2026-08-01',
  side: 'sell',
  quantity: 88.5,
  price: 1234.567,
}

const insertionTarget = {
  tradeDate: '2026-08-01',
  insertBeforeTradeId: 'source-trade',
}


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
  const draft = buildInlineTradeDraft(sourceTrade, insertionTarget)

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


test('按个股搜索支持代码和名称且忽略大小写与首尾空格', () => {
  const groups = [
    { symbol: '600519.SH', name: '贵州茅台' },
    { symbol: '000001.SZ', name: '平安银行' },
  ]

  assert.deepEqual(filterStockTradeGroups(groups, ''), groups)
  assert.deepEqual(filterStockTradeGroups(groups, ' 600519.sh '), [groups[0]])
  assert.deepEqual(filterStockTradeGroups(groups, '平安'), [groups[1]])
  assert.deepEqual(filterStockTradeGroups(groups, '不存在'), [])
})


test('三个流水视图共用草稿状态机，保存失败保留且成功后清除', () => {
  for (const view of ['flat', 'stock', 'date']) {
    let state = reduceLedgerInlineTradeState(null, {
      type: 'start',
      view,
      source: sourceTrade,
      target: insertionTarget,
    })
    assert.equal(state.view, view)
    assert.equal(state.draft.sourceTradeId, sourceTrade.id)

    const changedDraft = { ...state.draft, quantity: '100' }
    state = reduceLedgerInlineTradeState(state, { type: 'change', draft: changedDraft })
    assert.equal(state.draft.quantity, '100')

    const failedState = reduceLedgerInlineTradeState(state, {
      type: 'save-result',
      saved: false,
    })
    assert.deepEqual(failedState, state)
    assert.equal(reduceLedgerInlineTradeState(state, {
      type: 'save-result',
      saved: true,
    }), null)
  }
})


test('取消以及账户、日期和视图切换都会清除流水草稿', () => {
  const started = reduceLedgerInlineTradeState(null, {
    type: 'start',
    view: 'stock',
    source: sourceTrade,
    target: insertionTarget,
  })

  assert.equal(reduceLedgerInlineTradeState(started, { type: 'cancel' }), null)
  for (const context of ['account', 'date', 'view']) {
    assert.equal(reduceLedgerInlineTradeState(started, {
      type: 'context-changed',
      context,
    }), null)
  }
})


test('有效草稿创建交易后刷新缓存，并把插入位置传给 API', async () => {
  const calls = []
  const draft = buildInlineTradeDraft(sourceTrade, insertionTarget)
  const result = await persistInlineTradeDraft(draft, {
    createTrade: async payload => { calls.push(['create', payload]) },
    invalidate: async () => { calls.push(['invalidate']) },
  })

  assert.deepEqual(result, { status: 'saved' })
  assert.deepEqual(calls, [
    ['create', {
      account_id: 'account-1',
      symbol: '600519.SH',
      trade_date: '2026-08-01',
      side: 'sell',
      quantity: 88.5,
      price: 1234.567,
      insert_before_trade_id: 'source-trade',
    }],
    ['invalidate'],
  ])
})


test('API 创建失败时返回错误且不刷新缓存，供页面保留草稿并提示', async () => {
  const networkError = new TypeError('Failed to fetch')
  let invalidateCount = 0
  const result = await persistInlineTradeDraft(
    buildInlineTradeDraft(sourceTrade, insertionTarget),
    {
      createTrade: async () => { throw networkError },
      invalidate: async () => { invalidateCount += 1 },
    },
  )

  assert.deepEqual(result, { status: 'failed', error: networkError })
  assert.equal(invalidateCount, 0)
})


test('缓存刷新失败不允许重试创建同一交易', async () => {
  const refreshError = new Error('refresh failed')
  const result = await persistInlineTradeDraft(
    buildInlineTradeDraft(sourceTrade, insertionTarget),
    {
      createTrade: async () => undefined,
      invalidate: async () => { throw refreshError },
    },
  )

  assert.deepEqual(result, { status: 'saved', refreshError })
})


test('非法草稿不会调用 API 或刷新缓存', async () => {
  let dependencyCallCount = 0
  const invalidDraft = {
    ...buildInlineTradeDraft(sourceTrade, insertionTarget),
    quantity: '',
  }
  const result = await persistInlineTradeDraft(invalidDraft, {
    createTrade: async () => { dependencyCallCount += 1 },
    invalidate: async () => { dependencyCallCount += 1 },
  })

  assert.deepEqual(result, { status: 'invalid' })
  assert.equal(dependencyCallCount, 0)
})
