// Local-only compiled-app regression. No school, Google, or production access.
import { chromium } from '@playwright/test'
import { createServer } from 'node:http'
import { readFile } from 'node:fs/promises'
import { resolve, extname } from 'node:path'
import assert from 'node:assert/strict'

const requests = []
let fail = false
const server = createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost')
  if (url.pathname.startsWith('/api/')) {
    requests.push(url.pathname)
    res.setHeader('Content-Type', 'application/json')
    if (url.pathname === '/api/auth/oidc/exchange') {
      await new Promise(resolve => setTimeout(resolve, 200))
      if (fail) { res.writeHead(401); res.end('{}'); return }
      res.setHeader('Set-Cookie', 'session=synthetic; HttpOnly; SameSite=Lax; Path=/')
      res.end('{}'); return
    }
    if (url.pathname === '/api/auth/me' && req.headers.cookie?.includes('session=synthetic')) {
      res.end(JSON.stringify({ username: 'teacher', role: 'operator' })); return
    }
    res.writeHead(401); res.end('{}'); return
  }
  if (url.pathname === '/' && !url.search && req.headers.cookie?.includes('session=synthetic')) {
    res.setHeader('Content-Type', 'text/html')
    res.end('<h1>Verified authenticated document replacement</h1>'); return
  }
  const path = url.pathname.startsWith('/assets/') || url.pathname === '/sw.js' ? url.pathname : '/index.html'
  try {
    const data = await readFile(resolve('dist', `.${path}`))
    res.setHeader('Content-Type', ({ '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html' })[extname(path)] || 'application/octet-stream')
    res.end(data)
  } catch { res.writeHead(404); res.end() }
})
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
const origin = `http://127.0.0.1:${server.address().port}`
const browser = await chromium.launch({ headless: true, executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' })
try {
  for (const path of ['/', '/login']) {
    const context = await browser.newContext()
    const page = await context.newPage()
    requests.length = 0
    await page.goto(`${origin}${path}?oidc_code=synthetic`)
    await page.getByRole('heading', { name: 'Verified authenticated document replacement' }).waitFor()
    assert.equal(page.url(), `${origin}/`)
    assert.deepEqual(requests, ['/api/auth/oidc/exchange', '/api/auth/me'])
    console.log(`PASS compiled ${path} callback: one exchange, cookie verified, clean document replacement`)
    await context.close()
  }
  const context = await browser.newContext()
  const page = await context.newPage()
  fail = true
  requests.length = 0
  await page.goto(`${origin}/?oidc_code=synthetic`)
  await page.getByRole('alert').waitFor()
  assert.equal(page.url(), `${origin}/login`)
  assert.deepEqual(requests, ['/api/auth/oidc/exchange'])
  await page.screenshot({ path: '../.brain-output/oidc-failure.png' })
  console.log('PASS compiled failure: terminal error, no provider API traffic')

  // Seed a legacy callback cache key, then exercise real worker activation.
  await page.evaluate(async () => {
    for (const registration of await navigator.serviceWorker.getRegistrations()) await registration.unregister()
    const cache = await caches.open('odin-v1.9.16')
    await cache.put('/?oidc_code=synthetic-old', new Response('legacy'))
    await navigator.serviceWorker.register('/sw.js')
    await navigator.serviceWorker.ready
  })
  await page.waitForFunction(async () => {
    const cache = await caches.open('odin-v1.9.16')
    return !(await cache.keys()).some(request => new URL(request.url).searchParams.has('oidc_code'))
  })
  await page.reload()
  await page.waitForFunction(() => !!navigator.serviceWorker.controller)
  await page.goto(`${origin}/?oidc_code=synthetic-controlled`)
  await page.getByRole('alert').waitFor()
  assert.equal(await page.evaluate(async () => {
    const cache = await caches.open('odin-v1.9.16')
    return (await cache.keys()).some(request => new URL(request.url).searchParams.has('oidc_code'))
  }), false)
  console.log('PASS real service worker: legacy code purged, controlled callback not cached')
  await context.close()
} finally { await browser.close(); await new Promise(resolve => server.close(resolve)) }
