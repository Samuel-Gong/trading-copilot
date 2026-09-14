import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'


const source = await readFile(
  new URL('../src/components/StockPreviewDialog.tsx', import.meta.url),
  'utf8',
)

const abnormalMovesSource = await readFile(
  new URL('../src/pages/AbnormalMoves.tsx', import.meta.url),
  'utf8',
)

const apiSource = await readFile(
  new URL('../src/lib/api.ts', import.meta.url),
  'utf8',
)


test('个股弹窗仅在异动主开关开启时计算并复用一分钟新鲜缓存', () => {
  assert.match(source, /const abnormalEnabled = storage\.abnormalEnabled\.get\(false\)/)
  assert.match(source, /enabled: !!symbol && abnormalEnabled/)
  assert.match(source, /staleTime: 60_000/)
})


test('基准实时行情缺失时异动页显示不可用而不是零涨跌', () => {
  assert.match(apiSource, /bench_rt_pct: number \| null/)
  assert.match(abnormalMovesSource, /data\.bench_rt_pct == null/)
  assert.match(abnormalMovesSource, /基准指数今日不可用/)
})
