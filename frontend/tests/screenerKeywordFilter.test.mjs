import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import ts from 'typescript'


const source = await readFile(
  new URL('../src/lib/screenerKeywordFilter.ts', import.meta.url),
  'utf8',
)
const { outputText } = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2022,
  },
})
const moduleUrl = `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
const { matchesScreenerKeyword } = await import(moduleUrl)


test('空关键词不筛掉任何结果', () => {
  assert.equal(matchesScreenerKeyword({ symbol: '688169.SH', name: '石头科技' }, ''), true)
  assert.equal(matchesScreenerKeyword({ symbol: '688169.SH', name: '石头科技' }, '   '), true)
})


test('股票代码匹配忽略大小写和首尾空格', () => {
  const row = { symbol: '688169.SH', name: '石头科技' }
  assert.equal(matchesScreenerKeyword(row, '  688169.sh  '), true)
  assert.equal(matchesScreenerKeyword(row, '8169'), true)
})


test('股票名称支持子串匹配且缺失名称时安全返回', () => {
  assert.equal(matchesScreenerKeyword({ symbol: '688169.SH', name: '石头科技' }, '石头'), true)
  assert.equal(matchesScreenerKeyword({ symbol: '688169.SH' }, '石头'), false)
  assert.equal(matchesScreenerKeyword({ symbol: '688169.SH', name: '石头科技' }, '扫地机器人'), false)
})
