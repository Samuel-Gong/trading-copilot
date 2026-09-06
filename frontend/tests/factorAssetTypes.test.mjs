import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import ts from 'typescript'


const source = await readFile(
  new URL('../src/pages/backtest/factorCatalog.ts', import.meta.url),
  'utf8',
)
const { outputText } = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2022,
  },
})
const moduleUrl = `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
const { factorColumnsForAsset, factorNamesForAsset } = await import(moduleUrl)

const columns = [
  { id: 'momentum_20d', asset_types: ['stock', 'etf'] },
  { id: 'roe_latest', asset_types: ['stock'] },
]


test('ETF 因子目录隐藏股票专属财务因子', () => {
  assert.deepEqual(
    factorColumnsForAsset(columns, 'etf').map(item => item.id),
    ['momentum_20d'],
  )
  assert.deepEqual(
    factorColumnsForAsset(columns, 'stock').map(item => item.id),
    ['momentum_20d', 'roe_latest'],
  )
})


test('切换到 ETF 时清除已选股票财务因子', () => {
  assert.deepEqual(
    factorNamesForAsset(columns, ['roe_latest', 'momentum_20d'], 'etf'),
    ['momentum_20d'],
  )
})


test('三个因子入口统一使用资产类型过滤器', async () => {
  for (const name of ['FactorBacktest.tsx', 'FactorDiscovery.tsx', 'MiningWorkbench.tsx']) {
    const page = await readFile(new URL(`../src/pages/backtest/${name}`, import.meta.url), 'utf8')
    assert.match(page, /factorColumnsForAsset/)
  }
})
