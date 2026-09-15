import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import test from 'node:test'
import vm from 'node:vm'

import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ts from 'typescript'

// 渲染页面实际使用的单元格,不靠源码字符串匹配推断控件是否可操作。
const source = await readFile(new URL('../src/pages/Portfolio.tsx', import.meta.url), 'utf8')
const tree = ts.createSourceFile('Portfolio.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
const names = new Set(['TradeDateCell', 'TradeExecutionCell', 'SortableTradeRow', 'TradeInsertionButton'])
const declarations = tree.statements
  .filter(node => ts.isFunctionDeclaration(node) && names.has(node.name?.text))
  .map(node => `export ${node.getText(tree)}`).join('\n')
const compiled = ts.transpileModule(`
import { useState } from 'react'
import { Pencil, X, Check, GripVertical, Plus } from 'lucide-react'
import { useSortable } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
const formatPrice = String
const formatQuantity = String
const cn = (...values) => values.filter(Boolean).join(' ')
${declarations}
`, { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText
const module = { exports: {} }
vm.runInNewContext(compiled, { module, exports: module.exports, require: createRequire(import.meta.url) })
const { TradeDateCell, TradeExecutionCell, SortableTradeRow } = module.exports
const trade = { id: 'synthetic', trade_date: '2026-07-30', quantity: 100, price: 10.123 }
const render = (Component, value) => renderToStaticMarkup(React.createElement(Component, {
  trade: value, busy: false, tradingDates: [], onSave() {},
}))


test('来源成交保留日期和成交值,不渲染不可用的编辑按钮并解释原因', () => {
  const value = { ...trade, source_record_id: 'synthetic-fingerprint' }
  const date = render(TradeDateCell, value)
  const execution = render(TradeExecutionCell, value)
  assert.doesNotMatch(date + execution, /<button/)
  assert.match(date, /2026-07-30/)
  assert.match(date, /不支持手工修改/)
  assert.match(execution, /10.123/)
  assert.match(execution, /可以校准费用或删除记录/)
})


test('旧手工流水继续提供日期与数量价格编辑', () => {
  assert.match(render(TradeDateCell, trade), /<button[^>]*title="修改交易日期"/)
  assert.match(render(TradeExecutionCell, trade), /<button[^>]*title="修改成交数量和成交价"/)
})


test('来源行不能拖动或在行间补录,手工行仍有补录入口', () => {
  const row = value => renderToStaticMarkup(React.createElement(SortableTradeRow, {
    id: value.id, trade: value, busy: false, insertionDisabled: false,
    insertionTarget: { tradeDate: value.trade_date }, onInsertTrade() {},
  }, React.createElement('td', null, '合成行')))
  const imported = row({ ...trade, source_record_id: 'synthetic-fingerprint' })
  assert.match(imported, /disabled=""/)
  assert.match(imported, /不支持手工重排/)
  assert.doesNotMatch(imported, /在此处插入一条明细/)
  assert.match(row(trade), /在此处插入一条明细/)
})


test('费用对话框的估算请求与后端重估使用相同原始成交金额', async () => {
  const dialog = await readFile(new URL('../src/pages/portfolio/TradeCostDialog.tsx', import.meta.url), 'utf8')
  const parsed = ts.createSourceFile('TradeCostDialog.tsx', dialog, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  let estimate
  const visit = node => {
    if (ts.isFunctionDeclaration(node) && node.name?.text === 'estimate') estimate = node.getText(parsed)
    ts.forEachChild(node, visit)
  }
  visit(parsed)
  assert.ok(estimate)
  for (const value of [trade, { ...trade, amount: 1012.34 }]) {
    let request
    const context = {
      trade: value, busy: null, setBusy() {}, setFee() {}, setTax() {},
      api: { async portfolioTradeEstimate(body) { request = body; return { fee: 5, tax: 0 } } },
    }
    await vm.runInNewContext(`${estimate}; estimate()`, context)
    assert.equal(request.price, value.amount != null ? 10.1234 : 10.123)
    assert.equal(request.quantity, 100)
  }
})
