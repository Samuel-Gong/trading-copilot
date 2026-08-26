import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'


const source = await readFile(
  new URL('../src/pages/Portfolio.tsx', import.meta.url),
  'utf8',
)

const countOccurrences = value => source.split(value).length - 1


test('空流水提供添加第一条交易入口', () => {
  assert.match(
    source,
    /visibleTrades\.length === 0 \? \([\s\S]*添加第一条交易[\s\S]*\) : tradesView === 'date'/,
  )
})


test('全部、按个股和按日期三个视图都接入行间补录', () => {
  assert.equal(countOccurrences('onInsertTrade={startLedgerInlineTrade}'), 3)
  assert.match(source, /insertionTarget=\{flatTradeInsertionTargets\[index\]\}/)
  assert.match(source, /insertionTargets=\{buildTradeInsertionTargets\(group\.items, group\.date\)\}/)
  assert.match(source, /insertionTargets=\{buildTradeInsertionTargets\(group\.items, asOf\)\}/)
})
