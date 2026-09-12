import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'

const requireFromFrontend = createRequire(new URL('../../frontend/package.json', import.meta.url))
const { chromium } = requireFromFrontend('@playwright/test')

const baseURL = process.env.EDU_FRONTEND_URL || 'http://127.0.0.1:4173'
const output = process.env.EDU_PRIVACY_BROWSER_OUTPUT || 'artifacts/privacy-browser.json'
const fixtureRoot = new URL('../accessibility/fixtures/', import.meta.url)
const canary = 'student-browser-canary@school.test'

function loadFixture(name, seen = new Set()) {
  if (seen.has(name)) throw new Error(`fixture include cycle: ${[...seen, name].join(' -> ')}`)
  const document = JSON.parse(fs.readFileSync(new URL(name, fixtureRoot), 'utf8'))
  const nextSeen = new Set([...seen, name])
  return {
    ...Object.assign({}, ...(document.$include || []).map(included => loadFixture(included, nextSeen))),
    ...Object.fromEntries(Object.entries(document).filter(([key]) => !key.startsWith('$'))),
  }
}

const declared = { ...loadFixture('fleet.json'), ...loadFixture('admin.json') }
const state = { authenticated: false, erased: false }
const unexpected = []
const requests = []

async function apiRoute(route) {
  const request = route.request()
  const url = new URL(request.url())
  const key = `${request.method()} ${url.pathname}`
  requests.push(`${key}${url.search}`)

  if (key === 'POST /api/auth/login') {
    state.authenticated = true
    state.erased = false
    return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
  }
  if (key === 'POST /api/auth/logout') {
    state.authenticated = false
    return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
  }
  if (key === 'DELETE /api/users/1/erase') {
    state.authenticated = false
    state.erased = true
    return route.fulfill({ status: 200, contentType: 'application/json', body: '{"status":"erased"}' })
  }
  if (key === 'GET /api/auth/me') {
    const body = state.authenticated
      ? { id: 1, username: 'admin@school.test', email: 'admin@school.test', role: 'admin', group_id: 1 }
      : { detail: 'Not authenticated' }
    return route.fulfill({ status: state.authenticated ? 200 : 401, contentType: 'application/json', body: JSON.stringify(body) })
  }
  if (key === 'GET /api/permissions') {
    const body = { page_access: { settings: ['admin'], audit: ['admin'] }, action_access: { 'jobs.approve': ['admin', 'operator'] } }
    return route.fulfill({ status: state.authenticated ? 200 : 401, contentType: 'application/json', body: JSON.stringify(body) })
  }
  if (key === 'POST /api/auth/ws-token') {
    return route.fulfill({ status: state.authenticated ? 200 : 401, contentType: 'application/json', body: JSON.stringify(state.authenticated ? { token: '' } : { detail: 'Not authenticated' }) })
  }

  if (!Object.hasOwn(declared, key)) {
    unexpected.push(`${key}${url.search}`)
    return route.fulfill({ status: 599, contentType: 'application/json', body: '{"detail":"undeclared privacy fixture endpoint"}' })
  }
  const value = declared[key]
  return route.fulfill({ status: value.$status ?? 200, contentType: 'application/json', body: JSON.stringify(value.$body ?? value) })
}

async function seedSensitiveStores(page, { bulk = false } = {}) {
  await page.evaluate(async ({ marker, seedBulk }) => {
    localStorage.setItem('odin_user', marker)
    localStorage.setItem('rbac_permissions', marker)
    sessionStorage.setItem('access_token', marker)
    localStorage.setItem('odin-theme', 'light')
    if (!seedBulk) return
    const cache = await caches.open('odin-sensitive-test')
    await cache.put('/sensitive-test', new Response(marker))
    await new Promise((resolve, reject) => {
      const request = indexedDB.open('odin-sensitive-test', 1)
      request.onupgradeneeded = () => request.result.createObjectStore('records')
      request.onerror = () => reject(request.error)
      request.onsuccess = () => {
        const database = request.result
        const transaction = database.transaction('records', 'readwrite')
        transaction.objectStore('records').put(marker, 'identity')
        transaction.oncomplete = () => { database.close(); resolve() }
        transaction.onerror = () => reject(transaction.error)
      }
    })
  }, { marker: canary, seedBulk: bulk })
}

async function storageSnapshot(page) {
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      return await page.evaluate(async marker => ({
        local: Object.fromEntries(Object.entries(localStorage)),
        session: Object.fromEntries(Object.entries(sessionStorage)),
        caches: await caches.keys(),
        databases: indexedDB.databases ? (await indexedDB.databases()).map(item => item.name).filter(Boolean) : [],
        containsCanary: JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)]).includes(marker),
      }), canary)
    } catch (error) {
      if (!String(error).includes('Execution context was destroyed') || attempt === 2) throw error
      await page.waitForLoadState('domcontentloaded')
      await page.waitForTimeout(100)
    }
  }
  throw new Error('browser storage snapshot did not stabilize')
}

function storageIsSafe(snapshot, requireBulkClear = false) {
  const banned = ['odin_user', 'rbac_permissions', 'odin_token', 'access_token', 'refresh_token', 'mfa_token', 'reset_token']
  return !snapshot.containsCanary
    && banned.every(key => !(key in snapshot.local) && !(key in snapshot.session))
    && snapshot.local['odin-theme'] === 'light'
    && (!requireBulkClear || (!snapshot.caches.includes('odin-sensitive-test') && !snapshot.databases.includes('odin-sensitive-test')))
}

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] })
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
const page = await context.newPage()
page.setDefaultTimeout(5000)
await page.route('**/api/**', apiRoute)
const results = []

async function check(id, name, action) {
  try {
    const detail = await action()
    results.push({ id, name, pass: true, detail })
  } catch (error) {
    results.push({ id, name, pass: false, error: String(error) })
  }
}

async function login() {
  await page.goto(`${baseURL}/login`, { waitUntil: 'domcontentloaded' })
  await page.getByLabel(/username/i).fill('admin@school.test')
  await page.getByLabel(/password/i).fill('Synthetic-Browser-Only-2026!')
  await page.getByRole('button', { name: /sign in/i }).click()
  await page.getByRole('heading', { name: /Dashboard/i }).waitFor()
}

await check('P-B01', 'fresh login keeps identity and RBAC memory-only', async () => {
  await page.addInitScript(() => localStorage.setItem('odin-theme', 'light'))
  await login()
  const snapshot = await storageSnapshot(page)
  if (!storageIsSafe(snapshot)) throw new Error(`unsafe storage after login: ${JSON.stringify(snapshot)}`)
  return snapshot
})

await check('P-B02', 'reload rehydrates from the HttpOnly-backed API', async () => {
  const before = requests.filter(item => item.startsWith('GET /api/auth/me')).length
  await page.reload({ waitUntil: 'domcontentloaded' })
  await page.getByRole('heading', { name: /Dashboard/i }).waitFor()
  const after = requests.filter(item => item.startsWith('GET /api/auth/me')).length
  const snapshot = await storageSnapshot(page)
  if (after <= before || !storageIsSafe(snapshot)) throw new Error('reload did not re-fetch identity safely')
  return { identityRequestsAdded: after - before, storage: snapshot }
})

await check('P-B03', 'new context purges legacy keys but preserves preferences', async () => {
  const legacyContext = await browser.newContext()
  await legacyContext.addInitScript(marker => {
    localStorage.setItem('odin_user', marker)
    localStorage.setItem('rbac_permissions', marker)
    sessionStorage.setItem('access_token', marker)
    localStorage.setItem('odin-theme', 'light')
  }, canary)
  const legacyPage = await legacyContext.newPage()
  await legacyPage.route('**/api/**', apiRoute)
  await legacyPage.goto(`${baseURL}/login`, { waitUntil: 'domcontentloaded' })
  const snapshot = await storageSnapshot(legacyPage)
  await legacyContext.close()
  if (!storageIsSafe(snapshot)) throw new Error(`legacy keys survived bootstrap: ${JSON.stringify(snapshot)}`)
  return snapshot
})

await check('P-B04', 'logout clears legacy, Cache Storage, and IndexedDB state', async () => {
  await seedSensitiveStores(page, { bulk: true })
  await page.getByRole('button', { name: 'Logout', exact: true }).first().click()
  await page.getByRole('alertdialog', { name: /Confirm Logout/i }).getByRole('button', { name: 'Logout', exact: true }).click()
  await page.waitForURL('**/login')
  const snapshot = await storageSnapshot(page)
  if (!storageIsSafe(snapshot, true)) throw new Error(`sensitive stores survived logout: ${JSON.stringify(snapshot)}`)
  return snapshot
})

await check('P-B05', 'session expiry fails closed and clears sensitive stores', async () => {
  await login()
  await seedSensitiveStores(page, { bulk: true })
  state.authenticated = false
  await page.reload({ waitUntil: 'domcontentloaded' })
  await page.waitForURL('**/login')
  const snapshot = await storageSnapshot(page)
  if (!storageIsSafe(snapshot, true)) throw new Error(`sensitive stores survived session expiry: ${JSON.stringify(snapshot)}`)
  return snapshot
})

await check('P-B06', 'erasure clears browser stores and returns to login', async () => {
  await login()
  await page.goto(`${baseURL}/settings`, { waitUntil: 'domcontentloaded' })
  await page.getByRole('button', { name: 'System', exact: true }).click()
  await page.getByText('Privacy & Data').waitFor()
  await seedSensitiveStores(page, { bulk: true })
  await page.getByRole('button', { name: /Erase My Data/i }).click()
  await page.getByRole('button', { name: /Confirm Erase/i }).click()
  await page.waitForURL('**/login')
  const snapshot = await storageSnapshot(page)
  if (!state.erased || !storageIsSafe(snapshot, true)) throw new Error(`erasure lifecycle failed: ${JSON.stringify(snapshot)}`)
  return snapshot
})

await check('P-B07', 'post-erasure protected navigation remains unauthenticated', async () => {
  await page.goto(`${baseURL}/`, { waitUntil: 'domcontentloaded' })
  await page.waitForURL('**/login')
  await page.getByRole('button', { name: /sign in/i }).waitFor()
  await page.waitForTimeout(100)
  const snapshot = await storageSnapshot(page)
  if (!storageIsSafe(snapshot, true)) throw new Error(`post-erasure navigation restored sensitive state: ${JSON.stringify(snapshot)}`)
  return snapshot
})

await context.close()
await browser.close()
const result = {
  tool: { playwright: '1.58.0' },
  executed: results.length,
  unexpectedRequests: unexpected,
  requests,
  results,
}
fs.mkdirSync(path.dirname(output), { recursive: true })
fs.writeFileSync(output, `${JSON.stringify(result, null, 2)}\n`)
const failures = results.filter(item => !item.pass)
if (unexpected.length || failures.length || results.length !== 7) process.exitCode = 1
else console.log('7 passed in compiled browser privacy lifecycle')
