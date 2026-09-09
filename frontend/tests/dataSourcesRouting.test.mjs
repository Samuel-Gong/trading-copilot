import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const routingSource = readFileSync(
  new URL('../src/pages/settings/DataSources.tsx', import.meta.url),
  'utf8',
)
const editorSource = readFileSync(
  new URL('../src/pages/settings/DataSourceEditor.tsx', import.meta.url),
  'utf8',
)

test('数据源一键套用包含 full_minute 路由', () => {
  assert.match(
    routingSource,
    /full_minute_data_provider:\s*pick\('full_minute'\)/,
  )
})

test('实时比例字段要求显式选择单位', () => {
  assert.match(editorSource, /pct_unit/)
  assert.match(editorSource, /小数（0\.0366 表示 3\.66%）/)
  assert.match(editorSource, /百分数（3\.66 表示 3\.66%）/)
})

test('新增数据源保存后不得读取 undefined 名称的配置', () => {
  assert.match(editorSource, /if\s*\(existingName\)\s*fetchCfg\.refetch\(\)/)
})
