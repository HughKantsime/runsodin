/** Run from repository root. Uses installed dependencies only; never installs Chromium.
 * ODIN_FOUNDRY_PYTHON=/path/to/venv/bin/python node tests/foundry_browser/screenshots.mjs
 * Optional: --baseline=/path/to/2.0.42-capture (compares decoded Classic pixels).
 */
import { createRequire } from 'node:module'
import { spawn } from 'node:child_process'
import { mkdtemp, mkdir, readFile, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import net from 'node:net'
import { fileURLToPath } from 'node:url'
import { randomBytes } from 'node:crypto'
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const require = createRequire(path.join(root, 'frontend/package.json'))
const { chromium } = require('@playwright/test')
const AxeBuilder = require('@axe-core/playwright').default
const python = process.env.ODIN_FOUNDRY_PYTHON || 'python3'
const output = path.resolve(process.argv.find(a => a.startsWith('--output='))?.slice(9) || path.join(root, 'artifacts/foundry-part-a'))
const baseline = process.argv.find(a => a.startsWith('--baseline='))?.slice(11)
const report = { fixture: 'Disposable ODIN; synthetic DB seed; hardware transports unavailable', screenshots: [], axe: [], failures: [], classicComparison: baseline ? 'pending' : 'not run: pass --baseline with a 2.0.42 capture' }
const children = []
const directory = await mkdtemp(path.join(tmpdir(), 'odin-foundry-review-'))
await mkdir(output, { recursive: true })
const password = randomBytes(24).toString('base64url')
const reviewTime = process.argv.find(a => a.startsWith('--now='))?.slice(6) || '2026-11-04T12:00:00Z'
const delay = ms => new Promise(resolve => setTimeout(resolve, ms))
async function port() { const server = net.createServer(); await new Promise(resolve => server.listen(0, '127.0.0.1', resolve)); const n = server.address().port; await new Promise(resolve => server.close(resolve)); return n }
async function start(setup = false) {
  const n = await port()
  const dir = path.join(directory, setup ? 'setup' : 'seeded')
  const env = Object.fromEntries(Object.entries(process.env).filter(([k]) => !/^(ODIN_|DATABASE_|LICENSE_|ENCRYPTION_KEY|API_KEY|JWT_SECRET)/.test(k)))
  const child = spawn(python, [path.join(root, 'tests/foundry_browser/app_fixture.py'), dir, String(n), ...(setup ? ['--setup'] : [])], {
    cwd: directory, env: { ...env, PYTHONDONTWRITEBYTECODE: '1', ODIN_FOUNDRY_TEST_PASSWORD: password, ODIN_FOUNDRY_REVIEW_TIME: reviewTime }, stdio: ['ignore', 'pipe', 'pipe'],
  })
  children.push(child)
  let diagnostic = ''
  child.stdout.on('data', chunk => { diagnostic += chunk })
  child.stderr.on('data', chunk => { diagnostic += chunk })
  const url = `http://127.0.0.1:${n}`
  for (let attempt = 0; attempt < 300; attempt++) {
    if (child.exitCode !== null) throw new Error(`Fixture exited (${child.exitCode}). ${diagnostic.replaceAll(password, '[redacted]').slice(-3000)}`)
    try { if ((await fetch(url + '/health')).ok) return { url, dir } } catch {}
    await delay(100)
  }
  throw new Error('Disposable app did not become healthy')
}
// Explicit route inventory. Redirects retain their own screenshot names. Part B pages
// are captured for review, not claimed as restyled by Part A.
const routes = ['/', '/printers', '/printers/101', '/cameras', '/cameras/102', '/timeline', '/jobs', '/upload', '/models', '/profiles', '/spools', '/consumables', '/orders', '/products', '/calculator', '/analytics', '/utilization', '/print-log', '/alerts', '/detections', '/education', '/education-reports', '/timelapses', '/archives', '/projects', '/settings', '/admin', '/permissions', '/branding', '/maintenance', '/audit', '/tv', '/overlay/101', '/missing-page', '/login', '/reset-password', '/setup']
const main = new Set(['/', '/printers', '/printers/101', '/cameras', '/cameras/102', '/timeline'])
let browser
try {
  await readFile(path.join(root, 'frontend/dist/index.html'))
  const app = await start()
  const setup = await start(true)
  const seed = JSON.parse(await readFile(path.join(app.dir, 'seed.json'), 'utf8'))
  report.seed = seed
  // Freeze only Date. Timers keep running so camera failure paths and queries settle.
  browser = await chromium.launch({ headless: true })
  for (const theme of ['classic', 'foundry']) for (const width of [1440, 768, 390]) {
    const context = await browser.newContext({ viewport: { width, height: 900 }, locale: 'en-US', timezoneId: 'America/New_York', reducedMotion: 'reduce', serviceWorkers: 'block' })
    await context.route('**/*', route => {
      const u = new URL(route.request().url())
      return ['127.0.0.1', 'localhost'].includes(u.hostname) || ['data:', 'blob:'].includes(u.protocol) ? route.continue() : route.abort()
    })
    await context.addInitScript(({ theme, now }) => {
      localStorage.setItem('odin-theme', 'dark'); localStorage.setItem('odin-theme-family', theme)
      localStorage.setItem('odin-foundry-density', 'comfortable'); localStorage.setItem('odin_onboarding_complete', 'true')
      const OriginalDate = Date
      globalThis.Date = class extends OriginalDate { constructor(...args) { super(...(args.length ? args : [now])) } static now() { return now } }
    }, { theme, now: Date.parse(seed.now) })
    const login = await context.request.post(app.url + '/api/auth/login', { form: { username: 'reviewer', password } })
    if (!login.ok()) throw new Error(`Disposable login failed: ${login.status()}`)
    const page = await context.newPage()
    for (const route of routes) {
      const name = `${theme}-${width}-${route === '/' ? 'floor' : route.slice(1).replaceAll('/', '-')}`
      try {
        await page.goto((route === '/setup' ? setup.url : app.url) + route, { waitUntil: 'networkidle' })
        await page.evaluate(() => document.fonts.ready)
        if (route === '/' || route.startsWith('/cameras')) await page.waitForTimeout(18000)
        const filename = `${name}.png`
        await page.screenshot({ path: path.join(output, filename), fullPage: true, animations: 'disabled' })
        report.screenshots.push({ route, theme, width, filename, finalURL: new URL(page.url()).pathname })
        if (theme === 'foundry' && main.has(route)) {
          const result = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze()
          report.axe.push({ route, width, violations: result.violations })
          if (result.violations.length) report.failures.push(`${name}: ${result.violations.length} axe violations`)
        }
        if (theme === 'foundry' && route === '/') {
          await page.locator('.f-floor-lower').scrollIntoViewIfNeeded()
          await page.screenshot({ path: path.join(output, `${name}-cameras-schedule.png`), animations: 'disabled' })
          await page.locator('#main-content').evaluate(el => { el.scrollTop = 0 })
        }
        if (theme === 'foundry' && width !== 390 && ['/', '/printers'].includes(route)) {
          await page.getByRole('button', { name: 'Compact', exact: true }).click()
          await page.screenshot({ path: path.join(output, `${name}-compact.png`), fullPage: true, animations: 'disabled' })
          await page.getByRole('button', { name: 'Comfortable', exact: true }).click()
        }
      } catch (error) { report.failures.push(`${name}: ${error.message}`) }
    }
    await context.close()
  }
  if (baseline) {
    // Decode both PNGs through the browser canvas: compare pixels, not PNG metadata.
    const page = await browser.newPage()
    for (const capture of report.screenshots.filter(s => s.theme === 'classic')) {
      try {
        const images = await Promise.all([output, baseline].map(async dir => 'data:image/png;base64,' + (await readFile(path.join(dir, capture.filename))).toString('base64')))
        const same = await page.evaluate(async images => {
          const pixels = await Promise.all(images.map(src => new Promise((resolve, reject) => {
            const img = new Image(); img.onerror = reject; img.onload = () => { const c = document.createElement('canvas'); c.width = img.width; c.height = img.height; const ctx = c.getContext('2d'); ctx.drawImage(img, 0, 0); resolve({ w: c.width, h: c.height, data: ctx.getImageData(0, 0, c.width, c.height).data }) }; img.src = src
          })))
          return pixels[0].w === pixels[1].w && pixels[0].h === pixels[1].h && pixels[0].data.every((v, i) => v === pixels[1].data[i])
        }, images)
        if (!same) report.failures.push(`${capture.filename}: Classic pixels differ from baseline`)
      } catch (error) { report.failures.push(`${capture.filename}: baseline unavailable: ${error.message}`) }
    }
    report.classicComparison = report.failures.some(f => /Classic pixels|baseline unavailable/.test(f)) ? 'failed' : 'passed'
    await page.close()
  }
} catch (error) {
  report.failures.push(error.message.replaceAll(password, '[redacted]'))
} finally {
  await browser?.close()
  for (const child of children) {
    child.kill('SIGTERM')
    await Promise.race([new Promise(resolve => child.once('exit', resolve)), delay(3000)])
    if (child.exitCode === null) child.kill('SIGKILL')
  }
  await rm(directory, { recursive: true, force: true })
  await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2))
}
console.log(`Saved ${report.screenshots.length} screenshots; ${report.failures.length} failures. ${path.join(output, 'report.json')}`)
process.exitCode = report.failures.length ? 1 : 0
