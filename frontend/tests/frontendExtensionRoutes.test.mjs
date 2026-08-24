import assert from 'node:assert/strict'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { after, test } from 'node:test'

import React from 'react'
import { createServer } from 'vite'


const frontendRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const vite = await createServer({
  appType: 'custom',
  configFile: false,
  logLevel: 'silent',
  root: frontendRoot,
  server: { middlewareMode: true, ws: false },
})
after(async () => vite.close())

test('全部已注册核心页面都拒绝扩展路由覆盖', async () => {
  const routerSource = await vite.ssrLoadModule('/src/router.tsx?raw')
  const source = routerSource.default
  const block = source.match(/const CORE_ROUTE_PATHS = new Set\(\[([\s\S]*?)\]\)/)
  assert.ok(block, '应能定位核心路由保留集')
  const reservedPaths = new Set(
    [...block[1].matchAll(/'([^']+)'/g)].map(match => match[1]),
  )
  const registry = await vite.ssrLoadModule('/src/extensions/registry.ts')
  const paths = ['/watch-pool', '/portfolio', '/daily-review']

  await registry.loadFrontendExtensions(Object.fromEntries(paths.map((path, index) => [
    `extension-${index}`,
    async () => ({
      default: {
        id: `conflict-${index}`,
        apiVersion: 1,
        routes: [{ id: `route-${index}`, path, component: () => React.createElement('div') }],
      },
    }),
  ])))
  registry.finalizeFrontendExtensions(reservedPaths)

  assert.deepEqual(registry.getFrontendExtensionRoutes(), [])
  assert.deepEqual(
    registry.getFrontendExtensionLoadErrors().map(error => error.extensionId).sort(),
    ['conflict-0', 'conflict-1', 'conflict-2'],
  )
})
