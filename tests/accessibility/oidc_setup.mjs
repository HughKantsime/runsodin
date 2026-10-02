// Compiled-UI regression. HTTP persistence is independently covered by
// test_oidc_first_setup.py; this fixture exercises the browser setup workflow.
import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'
import assert from 'node:assert/strict'

const require = createRequire(new URL('../../frontend/package.json', import.meta.url))
const { chromium } = require('@playwright/test')
const baseURL = process.env.EDU_FRONTEND_URL || 'http://127.0.0.1:4173'
assert.ok(['localhost', '127.0.0.1'].includes(new URL(baseURL).hostname))
const output = process.env.EDU_OIDC_OUTPUT
assert.ok(output, 'EDU_OIDC_OUTPUT must identify an evidence directory')
fs.mkdirSync(output, { recursive: true })
const fixtures = Object.assign({}, ...['common', 'admin'].map(name => JSON.parse(fs.readFileSync(new URL(`./fixtures/${name}.json`, import.meta.url)))))
const browser = await chromium.launch({ headless: true })
const results = []
try {
  for (const width of [1440, 390]) {
    const context = await browser.newContext({ viewport: { width, height: 1000 } })
    const page = await context.newPage()
    const errors = []
    let organizations = []
    let config = { configured: false }
    page.on('pageerror', error => errors.push(String(error)))
    page.on('console', message => { if (message.type() === 'error') errors.push(message.text()) })
    await page.route('**/api/**', async route => {
      const request = route.request()
      const key = `${request.method()} ${new URL(request.url()).pathname}`
      let body = fixtures[key]
      if (key === 'GET /api/auth/me') body = { id: 1, username: 'fixture-admin', role: 'admin', group_id: null }
      if (key === 'GET /api/permissions') body = { page_access: { settings: ['admin'] }, action_access: { 'settings.edit': ['admin'] } }
      if (key === 'GET /api/orgs') body = organizations
      if (key === 'POST /api/orgs') {
        const org = { id: 1, name: request.postDataJSON().name, member_count: 0 }
        organizations = [org]
        body = { ...org, status: 'ok' }
      }
      if (key === 'GET /api/admin/oidc') body = config
      if (key === 'PUT /api/admin/oidc') {
        config = { ...request.postDataJSON(), configured: true, has_client_secret: true }
        delete config.client_secret
        body = { success: true }
      }
      if (key === 'GET /api/auth/mfa/status') body = { enabled: false }
      if (body === undefined) errors.push(`Undeclared fixture: ${key}`)
      await route.fulfill({ status: body === undefined ? 599 : 200, contentType: 'application/json', body: JSON.stringify(body?.$body ?? body ?? {}) })
    })
    await page.goto(`${baseURL}/settings`)
    await page.getByRole('button', { name: 'Access', exact: true }).click()
    await page.getByRole('button', { name: /Authentication \(OIDC & MFA\)/ }).click()
    await page.getByText(/No organizations yet\. Open Organizations/).waitFor()
    await page.getByLabel('OAuth client ID').fill('fixture-client')
    await page.getByRole('button', { name: 'Organizations', exact: true }).click()
    await page.getByRole('button', { name: 'New Org', exact: true }).click()
    await page.getByPlaceholder('Organization name').fill('Fixture School')
    await page.getByRole('button', { name: 'Create', exact: true }).click()
    await page.getByText('Fixture School', { exact: true }).waitFor()
    await page.getByRole('button', { name: 'Refresh tenant list' }).click()
    await page.getByRole('option', { name: 'Fixture School' }).waitFor({ state: 'attached' })
    await page.getByLabel('ODIN tenant for SSO users').selectOption('1')
    assert.equal(await page.getByLabel('OAuth client ID').inputValue(), 'fixture-client')
    await page.getByRole('button', { name: 'Save configuration' }).click()
    await page.getByText('Single sign-on configuration saved.', { exact: true }).waitFor()
    await page.reload()
    await page.getByRole('button', { name: 'Access', exact: true }).click()
    await page.getByRole('button', { name: /Authentication \(OIDC & MFA\)/ }).click()
    await page.getByLabel('OAuth client secret (configured)').waitFor()
    assert.equal(await page.getByLabel('OAuth client ID').inputValue(), 'fixture-client')
    assert.equal(await page.getByLabel('ODIN tenant for SSO users').inputValue(), '1')
    await page.getByLabel('ODIN tenant for SSO users').scrollIntoViewIfNeeded()
    await page.screenshot({ path: path.join(output, `oidc-${width}.png`), fullPage: true })
    assert.deepEqual(errors, [])
    results.push({ width, pass: true, workflow: 'create organization, refresh tenant, save SSO, reload', errors })
    await context.close()
  }
} finally {
  await browser.close()
  fs.writeFileSync(path.join(output, 'browser-results.json'), JSON.stringify(results, null, 2))
}
console.log(JSON.stringify(results))
