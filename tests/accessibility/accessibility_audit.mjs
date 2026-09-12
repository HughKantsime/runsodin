import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'

const requireFromFrontend = createRequire(new URL('../../frontend/package.json', import.meta.url))
const AxeBuilder = requireFromFrontend('@axe-core/playwright').default
const { chromium } = requireFromFrontend('@playwright/test')

const baseURL = process.env.EDU_FRONTEND_URL || 'http://127.0.0.1:4173'
const output = process.env.EDU_ACCESSIBILITY_OUTPUT || 'artifacts/accessibility-raw.json'
const fixtureRoot = new URL('./fixtures/', import.meta.url)

function loadFixture(name, seen = new Set()) {
  if (seen.has(name)) throw new Error(`fixture include cycle: ${[...seen, name].join(' -> ')}`)
  const document = JSON.parse(fs.readFileSync(new URL(name, fixtureRoot), 'utf8'))
  const nextSeen = new Set([...seen, name])
  const inherited = Object.assign(
    {},
    ...(document.$include || []).map(included => loadFixture(included, nextSeen)),
  )
  return { ...inherited, ...Object.fromEntries(Object.entries(document).filter(([key]) => !key.startsWith('$'))) }
}

const fixtureDocuments = new Map()
function fixtureDocument(name) {
  if (!fixtureDocuments.has(name)) fixtureDocuments.set(name, loadFixture(name))
  return fixtureDocuments.get(name)
}

const rows = [
  { id: 'A01', route: '/login', fixture: 'anonymous.json', personas: ['anonymous'], themes: ['dark', 'high-contrast'], viewports: [[1440, 900], [390, 844]] },
  { id: 'A02', route: '/', fixture: 'fleet.json', personas: ['admin', 'operator', 'viewer'], themes: ['dark', 'light', 'high-contrast'], viewports: [[1440, 900], [768, 1024], [390, 844]] },
  { id: 'A03', routes: ['/printers', '/printers/1'], fixture: 'fleet.json', personas: ['operator', 'viewer'], themes: ['dark', 'high-contrast'], viewports: [[1440, 900], [390, 844]] },
  { id: 'A04', route: '/jobs', fixture: 'jobs.json', personas: ['admin', 'operator', 'viewer'], themes: ['dark', 'light', 'high-contrast'], viewports: [[1440, 900], [390, 844]] },
  { id: 'A05', route: '/upload', fixture: 'jobs.json', personas: ['viewer'], themes: ['dark', 'high-contrast'], viewports: [[1440, 900], [390, 844]] },
  { id: 'A06', route: '/settings', tab: 'Access', fixture: 'admin.json', personas: ['admin'], themes: ['light', 'high-contrast'], viewports: [[1440, 900], [390, 844]] },
  { id: 'A07', route: '/settings', tab: 'System', fixture: 'admin.json', personas: ['admin'], themes: ['dark', 'high-contrast'], viewports: [[1440, 900], [390, 844]] },
  { id: 'A08', route: '/education-reports', fixture: 'reports.json', personas: ['admin', 'operator'], themes: ['dark', 'high-contrast'], viewports: [[1440, 900], [390, 844]] },
]

function fixture(name, method, path, persona) {
  const role = persona === 'anonymous' ? 'viewer' : persona
  const values = fixtureDocument(name)
  const key = `${method} ${path}`
  if (!Object.hasOwn(values, key)) {
    return { declared: false, status: 599, body: { error: 'undeclared accessibility fixture endpoint' } }
  }
  const declared = values[key]
  if (declared?.$dynamic === 'current_user') {
    return { declared: true, status: 200, body: { id: 1, username: `${role}@school.test`, email: `${role}@school.test`, role, group_id: 1 } }
  }
  if (declared?.$dynamic === 'permissions') {
    return { declared: true, status: 200, body: { page_access: { settings: ['admin'], audit: ['admin'] }, action_access: { 'jobs.approve': ['admin', 'operator'] } } }
  }
  return { declared: true, status: declared.$status ?? 200, body: declared.$body ?? declared }
}

async function handleApiRoute(route, fixtureName, persona, unexpected, requests) {
  const url = new URL(route.request().url())
  const method = route.request().method()
  requests.push(`${method} ${url.pathname}${url.search}`)
  const response = fixture(fixtureName, method, url.pathname, persona)
  if (!response.declared) unexpected.push(`UNDECLARED ${method} ${url.pathname}${url.search}`)
  await route.fulfill({ status: response.status, contentType: 'application/json', body: JSON.stringify(response.body) })
}

async function preparePage(page, fixtureName, persona, theme, unexpected, requests) {
  await page.addInitScript(({ selectedTheme }) => {
    localStorage.setItem('odin-theme', selectedTheme === 'light' ? 'light' : 'dark')
  }, { selectedTheme: theme })
  await page.route('**/api/**', route => handleApiRoute(route, fixtureName, persona, unexpected, requests))
}

async function motionAndTargetMetrics(page, checkMotion, mobile) {
  return page.evaluate(({ shouldCheckMotion, isMobile }) => {
    const parseDuration = value => value.split(',').reduce((max, part) => {
      const text = part.trim()
      const milliseconds = text.endsWith('ms') ? Number.parseFloat(text) : Number.parseFloat(text) * 1000
      return Number.isFinite(milliseconds) ? Math.max(max, milliseconds) : max
    }, 0)
    let longestMotionMs = 0
    if (shouldCheckMotion) {
      for (const element of document.querySelectorAll('*')) {
        const style = getComputedStyle(element)
        longestMotionMs = Math.max(longestMotionMs, parseDuration(style.animationDuration), parseDuration(style.transitionDuration))
      }
    }
    const selector = isMobile
      ? 'header button, nav a, nav button, button[type="submit"], label[for="file-upload"]'
      : 'button[type="submit"], label[for="file-upload"]'
    const sizes = [...document.querySelectorAll(selector)]
      .filter(element => {
        const style = getComputedStyle(element)
        const box = element.getBoundingClientRect()
        return style.visibility !== 'hidden' && style.display !== 'none' && box.width > 0 && box.height > 0
      })
      .map(element => {
        const box = element.getBoundingClientRect()
        return { name: element.getAttribute('aria-label') || element.textContent?.trim().slice(0, 60) || element.tagName, width: box.width, height: box.height }
      })
    return {
      longestMotionMs,
      targetSizes: {
        measured: sizes.length,
        below24: sizes.filter(size => size.width < 24 || size.height < 24),
        below44: sizes.filter(size => size.width < 44 || size.height < 44).length,
      },
    }
  }, { shouldCheckMotion: checkMotion, isMobile: mobile })
}

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] })
const results = []
for (const row of rows) {
  for (const routePath of row.routes || [row.route]) {
  for (const persona of row.personas) {
    for (const theme of row.themes) {
      for (const [width, height] of row.viewports) {
        const context = await browser.newContext({ viewport: { width, height }, reducedMotion: ['A02', 'A04', 'A06', 'A07'].includes(row.id) ? 'reduce' : 'no-preference' })
        const page = await context.newPage()
        const consoleErrors = []
        const expectedConsoleErrors = []
        const unexpected = []
        const requests = []
        page.on('console', message => {
          if (message.type() !== 'error') return
          if (persona === 'anonymous' && message.text().includes('401 (Unauthorized)')) expectedConsoleErrors.push(message.text())
          else consoleErrors.push(message.text())
        })
        page.on('pageerror', error => consoleErrors.push(String(error)))
        await preparePage(page, row.fixture, persona, theme, unexpected, requests)
        const finding = { id: row.id, route: routePath, fixture: row.fixture, persona, theme, viewport: `${width}x${height}`, axeFindings: [], serious: [], assertions: [], consoleErrors, expectedConsoleErrors, unexpected, requests }
        try {
          await page.goto(`${baseURL}${routePath}`, { waitUntil: 'domcontentloaded' })
          if (theme === 'high-contrast') {
            await page.evaluate(() => document.documentElement.classList.add('high-contrast'))
          }
          await page.waitForTimeout(250)
          if (row.tab) {
            const tab = page.getByRole('button', { name: row.tab, exact: true })
            if (await tab.count()) await tab.first().click()
            await page.waitForTimeout(100)
          }
          if (row.id === 'A06') {
            await page.getByRole('button', { name: /Quotas & Restrictions/i }).click()
            await page.waitForTimeout(100)
          }
          const axe = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa']).analyze()
          finding.axeFindings = axe.violations.map(item => ({
            id: item.id,
            impact: item.impact,
            nodes: item.nodes.map(node => ({ target: node.target })),
          }))
          finding.serious = finding.axeFindings.filter(item => ['serious', 'critical'].includes(item.impact))
          finding.assertions.push({ name: 'page heading', pass: await page.locator('h1').count() > 0 })
          finding.assertions.push({ name: 'main landmark', pass: await page.locator('main').count() > 0 })
          if (row.id === 'A01') {
            finding.assertions.push({ name: 'login form and submit action', pass: await page.locator('form').count() > 0 && await page.getByRole('button', { name: /sign in/i }).count() > 0 })
            finding.assertions.push({ name: 'username label', pass: await page.getByLabel(/username/i).count() > 0 })
            finding.assertions.push({ name: 'password label', pass: await page.getByLabel(/password/i).count() > 0 })
          }
          if (row.id === 'A02') finding.assertions.push({ name: 'dashboard navigation', pass: await page.locator('nav').count() > 0 })
          if (row.id === 'A03') {
            const expectedHeading = routePath.endsWith('/1') ? /EDU Printer/i : /Printers/i
            finding.assertions.push({ name: 'printer surface ready', pass: await page.getByRole('heading', { name: expectedHeading }).count() > 0 })
          }
          if (row.id === 'A04') finding.assertions.push({ name: 'jobs table ready', pass: await page.getByRole('table').count() > 0 })
          if (row.id === 'A05') finding.assertions.push({ name: 'file input and upload action', pass: await page.locator('input[type=file]').count() > 0 && await page.locator('label[for="file-upload"]').count() > 0 })
          if (row.id === 'A06') finding.assertions.push({ name: 'access groups and quotas ready', pass: await page.getByRole('heading', { name: 'Groups', exact: true }).count() > 0 && await page.getByText(/Print Quotas/i).count() > 0 })
          if (row.id === 'A07') finding.assertions.push({ name: 'backup retention and privacy ready', pass: await page.getByText(/Database Backups/i).count() > 0 && await page.getByText(/Data Retention/i).count() > 0 && await page.getByText(/Privacy & Data/i).count() > 0 })
          if (row.id === 'A08') finding.assertions.push({ name: 'education report filters and export ready', pass: await page.getByRole('heading', { name: 'Usage Reports' }).count() > 0 && await page.getByRole('button', { name: /Export CSV/i }).count() > 0 })
          const checkMotion = ['A02', 'A04', 'A06', 'A07'].includes(row.id)
          const metrics = await motionAndTargetMetrics(page, checkMotion, width <= 390)
          finding.motion = { checked: checkMotion, longestMs: metrics.longestMotionMs }
          finding.targetSizes = metrics.targetSizes
          if (checkMotion) finding.assertions.push({ name: 'reduced motion <= 100ms', pass: metrics.longestMotionMs <= 100 })
          finding.assertions.push({ name: 'minimum 24px primary/mobile targets', pass: metrics.targetSizes.below24.length === 0 })
        } catch (error) {
          finding.consoleErrors.push(String(error))
        }
        finding.pass = finding.serious.length === 0 && finding.consoleErrors.length === 0 && finding.unexpected.length === 0 && finding.assertions.every(assertion => assertion.pass)
        if (!finding.pass) {
          const screenshotDir = path.join(path.dirname(output), 'accessibility-screenshots')
          fs.mkdirSync(screenshotDir, { recursive: true })
          const routeSlug = routePath.replaceAll('/', '_') || '_root'
          const screenshotPath = path.join(screenshotDir, `${row.id}-${routeSlug}-${persona}-${theme}-${width}x${height}.png`)
          await page.screenshot({ path: screenshotPath, fullPage: true })
          finding.screenshot = path.relative(path.dirname(output), screenshotPath)
        }
        results.push(finding)
        await context.close()
      }
    }
  }
  }
}

async function runKeyboardCase(id, route, fixtureName, persona, viewport, scenario) {
  const context = await browser.newContext({ viewport })
  const page = await context.newPage()
  const finding = { id, route, fixture: fixtureName, persona, theme: 'dark', viewport: `${viewport.width}x${viewport.height}`, axeFindings: [], serious: [], assertions: [], consoleErrors: [], unexpected: [], requests: [] }
  page.on('console', message => { if (message.type() === 'error') finding.consoleErrors.push(message.text()) })
  page.on('pageerror', error => finding.consoleErrors.push(String(error)))
  await preparePage(page, fixtureName, persona, 'dark', finding.unexpected, finding.requests)
  try {
    await page.goto(`${baseURL}${route}`, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(250)
    await scenario(page, finding.assertions)
  } catch (error) {
    finding.consoleErrors.push(String(error))
  }
  finding.pass = finding.consoleErrors.length === 0 && finding.unexpected.length === 0 && finding.assertions.length > 0 && finding.assertions.every(assertion => assertion.pass)
  if (!finding.pass) {
    const screenshotDir = path.join(path.dirname(output), 'accessibility-screenshots')
    fs.mkdirSync(screenshotDir, { recursive: true })
    const screenshotPath = path.join(screenshotDir, `${id}-${persona}-${viewport.width}x${viewport.height}.png`)
    await page.screenshot({ path: screenshotPath, fullPage: true })
    finding.screenshot = path.relative(path.dirname(output), screenshotPath)
  }
  results.push(finding)
  await context.close()
}

await runKeyboardCase('K01', '/', 'fleet.json', 'admin', { width: 1440, height: 900 }, async (page, assertions) => {
  await page.keyboard.press('Tab')
  assertions.push({ name: 'skip link is first focus target', pass: await page.evaluate(() => document.activeElement?.textContent?.trim() === 'Skip to content') })
  await page.keyboard.press('Enter')
  assertions.push({ name: 'skip link focuses main content', pass: await page.evaluate(() => document.activeElement?.id === 'main-content') })
})

await runKeyboardCase('K02', '/', 'fleet.json', 'admin', { width: 1440, height: 900 }, async (page, assertions) => {
  const start = page.getByRole('button', { name: /collapse sidebar/i })
  await start.focus()
  const reached = new Set()
  for (let index = 0; index < 30 && reached.size < 3; index += 1) {
    await page.keyboard.press('Tab')
    const item = await page.evaluate(() => {
      const active = document.activeElement
      return active?.closest('nav') && active instanceof HTMLAnchorElement ? active.textContent?.trim() : ''
    })
    if (item) reached.add(item)
  }
  assertions.push({ name: 'sidebar links are reachable in tab order', pass: reached.size >= 3, reached: [...reached] })
})

await runKeyboardCase('K03', '/', 'fleet.json', 'admin', { width: 1440, height: 900 }, async (page, assertions) => {
  const trigger = page.getByRole('button', { name: /collapse sidebar/i })
  await trigger.focus()
  await page.keyboard.press('?')
  const dialog = page.getByRole('dialog', { name: 'Keyboard Shortcuts' })
  await dialog.waitFor()
  assertions.push({ name: 'focus enters shortcut modal', pass: await dialog.evaluate((element) => element.contains(document.activeElement)) })
  const wrapped = await dialog.evaluate((element) => {
    const focusable = [...element.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')]
    const last = focusable.at(-1)
    if (!(last instanceof HTMLElement)) return false
    last.focus()
    return true
  })
  await page.keyboard.press('Tab')
  assertions.push({ name: 'modal traps forward tab', pass: wrapped && await dialog.evaluate((element) => element.contains(document.activeElement)) })
  await page.keyboard.press('Escape')
  await dialog.waitFor({ state: 'detached' })
  assertions.push({ name: 'escape closes modal and restores focus', pass: await trigger.evaluate((element) => document.activeElement === element) })
})

await runKeyboardCase('K04', '/upload', 'jobs.json', 'viewer', { width: 1440, height: 900 }, async (page, assertions) => {
  const input = page.locator('#file-upload')
  await page.locator('body').focus()
  let reached = false
  for (let index = 0; index < 80 && !reached; index += 1) {
    await page.keyboard.press('Tab')
    reached = await input.evaluate((element) => document.activeElement === element)
  }
  assertions.push({ name: 'upload chooser is keyboard reachable', pass: reached })
  assertions.push({ name: 'upload chooser has an accessible label', pass: await input.evaluate((element) => Boolean(element.labels?.[0]?.textContent?.trim())) })
})

await runKeyboardCase('K05', '/settings', 'admin.json', 'admin', { width: 1440, height: 900 }, async (page, assertions) => {
  await page.getByRole('button', { name: 'System', exact: true }).click()
  const trigger = page.getByRole('button', { name: /erase my data/i })
  await trigger.focus()
  await trigger.click()
  const confirmation = page.getByRole('alertdialog', { name: /confirm permanent erasure/i })
  await confirmation.waitFor()
  assertions.push({ name: 'destructive confirmation has alertdialog name and focus', pass: await confirmation.evaluate((element) => element.contains(document.activeElement)) })
  await page.keyboard.press('Escape')
  await confirmation.waitFor({ state: 'detached' })
  assertions.push({ name: 'destructive cancel restores trigger focus', pass: await trigger.evaluate((element) => document.activeElement === element) })
})

await runKeyboardCase('K06', '/', 'fleet.json', 'admin', { width: 1440, height: 900 }, async (page, assertions) => {
  const search = page.locator('#global-search:visible')
  await search.focus()
  await page.keyboard.type('?')
  assertions.push({ name: 'shortcut help does not steal input focus', pass: await search.evaluate((element) => document.activeElement === element && element.value === '?') })
  assertions.push({ name: 'shortcut dialog stays closed while typing', pass: await page.getByRole('dialog', { name: 'Keyboard Shortcuts' }).count() === 0 })
})
await browser.close()
fs.mkdirSync(path.dirname(output), { recursive: true })
fs.writeFileSync(output, JSON.stringify({ tool: { axe: '4.10.2', playwright: '1.58.0' }, results }, null, 2) + '\n')
if (results.some(result => !result.pass)) process.exitCode = 1
