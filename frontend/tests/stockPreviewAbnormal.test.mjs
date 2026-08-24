import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'


const source = await readFile(
  new URL('../src/components/StockPreviewDialog.tsx', import.meta.url),
  'utf8',
)


test('个股弹窗仅在异动主开关开启时计算并复用一分钟新鲜缓存', () => {
  assert.match(source, /const abnormalEnabled = storage\.abnormalEnabled\.get\(false\)/)
  assert.match(source, /enabled: !!symbol && abnormalEnabled/)
  assert.match(source, /staleTime: 60_000/)
})
