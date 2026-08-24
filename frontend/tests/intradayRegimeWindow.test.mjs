import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import ts from 'typescript'


async function importTypeScript(path) {
  const source = await readFile(new URL(path, import.meta.url), 'utf8')
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ES2022,
      target: ts.ScriptTarget.ES2022,
    },
  })
  const moduleUrl = `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
  return { source, module: await import(moduleUrl) }
}


test('北京时间分钟戳不得在前端重复增加八小时', async () => {
  const { module } = await importTypeScript('../src/lib/intraday-chart.ts')

  assert.equal(module.formatMinuteTime('2026-05-21T09:35:00'), '09:35')
  assert.equal(module.formatMinuteTime('2026-05-21 14:57:00'), '14:57')
})


test('阶段与主线查询把最近交易日 limit 传到 API 和缓存键', async () => {
  const regime = await readFile(new URL('../src/pages/Regime.tsx', import.meta.url), 'utf8')
  const api = await readFile(new URL('../src/lib/api.ts', import.meta.url), 'utf8')
  const queryKeys = await readFile(new URL('../src/lib/queryKeys.ts', import.meta.url), 'utf8')

  assert.match(regime, /QK\.regimePhases\(histRange\.start, histRange\.end, histRange\.limit\)/)
  assert.match(regime, /api\.regimePhases\(histRange\.start, histRange\.end, histRange\.limit\)/)
  assert.match(regime, /QK\.regimeMainline\(mainlineKind, histRange\.start, histRange\.end, histRange\.limit\)/)
  assert.match(regime, /api\.regimeMainline\(histRange\.start, histRange\.end, 10, mainlineKind, histRange\.limit\)/)
  assert.match(api, /regimePhases: \(start\?: string, end\?: string, limit\?: number\)/)
  assert.match(api, /regimeMainline: \(start\?: string, end\?: string, top = 10, kind: 'concept' \| 'industry' = 'concept', limit\?: number\)/)
  assert.match(queryKeys, /regimePhases:\s+\(start\?: string, end\?: string, limit\?: number\)/)
  assert.match(queryKeys, /regimeMainline:\s+\(kind: string, start\?: string, end\?: string, limit\?: number\)/)
})
