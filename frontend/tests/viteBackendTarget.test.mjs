import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import ts from 'typescript'


const source = await readFile(
  new URL('../viteBackendTarget.ts', import.meta.url),
  'utf8',
)
const { outputText } = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2022,
  },
})
const moduleUrl = `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`
const { buildBackendTarget } = await import(moduleUrl)


test('Vite 代理目标正确规范化 IPv6 字面量', () => {
  assert.equal(buildBackendTarget('::1', '3018'), 'http://[::1]:3018')
  assert.equal(buildBackendTarget('2001:db8::1', '4018'), 'http://[2001:db8::1]:4018')
  assert.equal(buildBackendTarget('[::1]', '3018'), 'http://[::1]:3018')
})


test('Vite 代理目标保留通配地址与旧端口兼容语义', () => {
  assert.equal(buildBackendTarget('::', undefined, '4018'), 'http://127.0.0.1:4018')
  assert.equal(buildBackendTarget('0.0.0.0', undefined), 'http://127.0.0.1:3018')
  assert.equal(buildBackendTarget('localhost', '5018'), 'http://localhost:5018')
})
