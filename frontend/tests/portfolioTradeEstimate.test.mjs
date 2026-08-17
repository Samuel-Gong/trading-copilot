import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import ts from 'typescript'


const source = await readFile(
  new URL('../src/pages/portfolio/tradeEstimate.ts', import.meta.url),
  'utf8',
)
const { outputText } = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2022,
  },
})
const moduleUrl = `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
const { mergeTradeEstimateIfCurrent } = await import(moduleUrl)


const requested = {
  symbol: '600000.SH',
  side: 'buy',
  quantity: 100,
  price: 10,
}


test('估算完成时合并到最新表单且不恢复已清除的插入锚点', () => {
  const current = {
    accountId: 'new-account',
    symbol: '600000.SH',
    tradeDate: '2026-08-02',
    side: 'buy',
    quantity: '100',
    price: '10',
    fee: '',
    tax: '',
    note: '',
    insertBeforeTradeId: undefined,
  }

  assert.deepEqual(
    mergeTradeEstimateIfCurrent(current, requested, { fee: 5, tax: 0.1 }),
    { ...current, fee: '5.00', tax: '0.10' },
  )
})


test('估算请求输入已变化时忽略过期响应', () => {
  for (const changed of [
    { symbol: '600519.SH' },
    { side: 'sell' },
    { quantity: '200' },
    { price: '11' },
  ]) {
    const current = {
      symbol: '600000.SH', side: 'buy', quantity: '100', price: '10',
      fee: '', tax: '', ...changed,
    }
    assert.equal(
      mergeTradeEstimateIfCurrent(current, requested, { fee: 5, tax: 0.1 }),
      current,
    )
  }
})
