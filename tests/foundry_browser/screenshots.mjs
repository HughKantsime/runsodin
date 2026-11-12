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
import { comparePixels, volatilePass } from './pixel_compare.mjs'
import { inspectFoundryLayout, collectPhoneLayout, judgePhoneLayout, phoneFindingCount, phoneFindingSummary, summarizePhoneLayout } from './layout_checks.mjs'
import { comparisonMaskDefinitions, measureComparisonMasks, comparisonMaskRectangles } from './comparison_masks.mjs'
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const require = createRequire(path.join(root, 'frontend/package.json'))
const { chromium } = require('@playwright/test')
const AxeBuilder = require('@axe-core/playwright').default
const python = process.env.ODIN_FOUNDRY_PYTHON || 'python3'
const output = path.resolve(process.argv.find(a => a.startsWith('--output='))?.slice(9) || path.join(root, 'artifacts/foundry-part-b'))
const baseline = process.argv.find(a => a.startsWith('--baseline='))?.slice(11)
const volatileFile = process.argv.find(a => a.startsWith('--volatile='))?.slice(11)
if (volatileFile && !baseline) throw new Error('--volatile requires --baseline')
const volatility = volatileFile ? JSON.parse(await readFile(volatileFile, 'utf8')) : null
if (volatility && (volatility.pixelTolerance !== 10 || !Array.isArray(volatility.classicDiffs))) throw new Error('Volatility report must be a base-vs-base report with pixelTolerance 10 and classicDiffs')
const volatileCaptures = new Map((volatility?.classicDiffs || []).map(capture => [capture.filename, capture]))
// Phone-width (<= 480 px) layout checks always run and are reported; findings only fail the run with
// --phone-fail (or ODIN_FOUNDRY_PHONE_FAIL=1). Default is report-only because the product is not clean yet.
const phoneFail = process.argv.includes('--phone-fail') || process.env.ODIN_FOUNDRY_PHONE_FAIL === '1'
const report = { pixelTolerance: 10, maskDefinitions: comparisonMaskDefinitions, comparisonMasks: [], classicDiffs: [], volatilePasses: [], volatility: volatileFile || null, fixture: 'Disposable ODIN; synthetic DB seed; hardware transports unavailable', screenshots: [], axe: [], layout: [], failures: [], classicComparison: baseline ? 'pending' : 'not run: pass --baseline with a 2.0.42 capture' }
report.phoneLayout = []
report.phoneSummary = null
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
// Polling and camera streams never become network-idle. Wait for document and
// rendered route content, then fonts and a bounded settle. Recharts' default
// animation is 1500ms; 2200ms after chart mount also works on the untouched base.
async function gotoReady(page, url, theme) {
  await page.goto(url, { waitUntil: 'load' })
  await page.waitForFunction(({ foundry }) => {
    const root = foundry ? document.querySelector('[data-foundry-page]')
      : document.querySelector('#main-content') || document.querySelector('main') || document.querySelector('#root')
    const text = root?.textContent?.trim()
    return !!text && !/^(loading|checking|redirecting)[.…\s]*$/i.test(text)
      && !root.querySelector('[aria-busy="true"]')
  }, { foundry: theme === 'foundry' })
  await page.evaluate(() => document.fonts.ready)
  await page.waitForTimeout(600)
  if (await page.locator('.recharts-wrapper').count()) await page.waitForTimeout(2200)
}
// The app scrolls inside #main-content. fullPage would stitch a tall, mostly
// empty document. Capture the viewport and walk the actual scrolling surface.
async function checkPhoneLayout(page, entry) {
  const data = await page.evaluate(collectPhoneLayout)
  const findings = judgePhoneLayout(data, { overlay: !!entry.overlay })
  const count = phoneFindingCount(findings)
  report.phoneLayout.push({ route: entry.route, theme: entry.theme, width: entry.width, filename: entry.filename, count, ...findings })
  if (count && phoneFail) report.failures.push(`${entry.filename}: phone layout findings (${phoneFindingSummary(findings)})`)
}
async function checkLayout(page, entry) {
  if (entry.width <= 480) await checkPhoneLayout(page, entry)
  if (entry.theme !== 'foundry') return
  const layout = await page.evaluate(inspectFoundryLayout)
  report.layout.push({ route: entry.route, width: entry.width, filename: entry.filename, ...layout })
  if (layout.smallText.length || layout.smallTargets.length || layout.overflow || layout.rightOverflow.length || layout.wrappedWords.length)
    report.failures.push(`${entry.filename}: Foundry type, target, overflow or mid-word wrap check failed`)
}
async function capture(page, entry, scroll = true) {
  await checkLayout(page, entry)
  const comparisonMasks = await page.evaluate(measureComparisonMasks, comparisonMaskDefinitions)
  await page.screenshot({ path: path.join(output, entry.filename), animations: 'disabled' })
  report.screenshots.push({ ...entry, comparisonMasks, finalURL: new URL(page.url()).pathname })
  if (!scroll) return
  const scroller = page.locator('#main-content')
  const internal = await scroller.count() > 0
  const dimensions = internal ? await scroller.evaluate(el => ({ height: el.clientHeight, total: el.scrollHeight }))
    : await page.evaluate(() => ({ height: innerHeight, total: document.documentElement.scrollHeight }))
  if (!dimensions.height) return
  for (let y = dimensions.height, part = 1; y < dimensions.total; y += dimensions.height, part++) {
    if (internal) await scroller.evaluate((el, top) => { el.scrollTop = top }, y)
    else await page.evaluate(top => scrollTo(0, top), y)
    const filename = entry.filename.replace(/\.png$/, `-scroll-${part}.png`)
    await checkLayout(page, { ...entry, filename })
    const comparisonMasks = await page.evaluate(measureComparisonMasks, comparisonMaskDefinitions)
    await page.screenshot({ path: path.join(output, filename), animations: 'disabled' })
    report.screenshots.push({ ...entry, filename, comparisonMasks, scrollPart: part, finalURL: new URL(page.url()).pathname })
  }
  if (internal) await scroller.evaluate(el => { el.scrollTop = 0 })
  else await page.evaluate(() => scrollTo(0, 0))
}
// Fail if a router entry is missing from the review inventory (including early returns).
const inventory = JSON.parse(await readFile(path.join(root, 'tests/foundry_browser/routes.json'), 'utf8'))
const routes = inventory.map(entry => entry.url)
const appSource = await readFile(path.join(root, 'frontend/src/App.tsx'), 'utf8')
const routerPaths = [...appSource.matchAll(/<Route path="([^"]+)"/g)].map(match => match[1])
const standalone = [...appSource.matchAll(/location.pathname === '([^']+)'/g)].map(match => match[1])
const declared = [...new Set([...routerPaths, ...standalone])].sort()
if (JSON.stringify(declared) !== JSON.stringify(inventory.map(entry => entry.route).sort())) throw new Error('Router and screenshot inventory differ')
report.routes = inventory
let browser
try {
  await readFile(path.join(root, 'frontend/dist/index.html'))
  const app = await start()
  const setup = await start(true)
  const seed = JSON.parse(await readFile(path.join(app.dir, 'seed.json'), 'utf8'))
  report.seed = seed
  // Freeze only Date. Timers keep running so camera failure paths and queries settle.
  browser = await chromium.launch({ headless: true })
  for (const theme of (process.argv.includes('--classic-only') ? ['classic'] : ['classic', 'foundry'])) for (const width of [1440, 768, 390]) {
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
    const drawerFamilies = new Set()
    for (const route of routes) {
      const name = `${theme}-${width}-${route === '/' ? 'floor' : route.slice(1).replaceAll('/', '-')}`
      try {
        await gotoReady(page, (route === '/setup' ? setup.url : app.url) + route, theme)
        await page.evaluate(() => document.fonts.ready)
        const expected = inventory.find(entry => entry.url === route).finalPath
        if (new URL(page.url()).pathname !== expected) throw new Error(`Expected ${expected}; landed on ${new URL(page.url()).pathname}`)
        if (theme === 'foundry' && await page.locator('[data-foundry-page]').count() !== 1) throw new Error('Missing Foundry page boundary')
        if (route === '/' || route.startsWith('/cameras')) await page.waitForTimeout(18000)
        const filename = `${name}.png`
        await capture(page, { route, theme, width, filename })
        if (theme === 'foundry') {
          const result = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze()
          report.axe.push({ route, width, violations: result.violations })
          if (result.violations.length) report.failures.push(`${name}: ${result.violations.length} axe violations`)
        }
        if (theme === 'foundry' && route === '/') {
          await page.locator('.f-floor-lower').scrollIntoViewIfNeeded()
          await capture(page, { route, theme, width, filename: `${name}-cameras-schedule.png` }, false)
          await page.locator('#main-content').evaluate(el => { el.scrollTop = 0 })
        }
        if (theme === 'foundry' && width !== 390 && ['/', '/printers'].includes(route)) {
          await page.getByRole('button', { name: 'Compact', exact: true }).click()
          await capture(page, { route, theme, width, filename: `${name}-compact.png` })
          await page.getByRole('button', { name: 'Comfortable', exact: true }).click()
        }
        if (width <= 480) {
          // Extra phone captures are marked `extra` so --baseline (Classic pixel comparison) ignores them.
          const family = new URL(page.url()).pathname.split('/')[1] || 'floor'
          const menu = page.getByRole('button', { name: 'Open menu', exact: true })
          if (!drawerFamilies.has(family) && await menu.isVisible()) {
            drawerFamilies.add(family)
            await menu.click()
            const closeMenu = page.getByRole('button', { name: 'Close menu', exact: true })
            await closeMenu.waitFor({ state: 'visible' })
            await page.waitForTimeout(500)
            await capture(page, { route, theme, width, filename: `${name}-drawer.png`, extra: true, overlay: true }, false)
            await closeMenu.click()
            await page.waitForTimeout(300)
          }
          if (route === '/jobs') {
            const opener = page.getByRole('button', { name: 'New Job', exact: true })
            if (await opener.isVisible()) {
              await opener.click()
              await page.getByRole('dialog', { name: 'Create New Job', exact: true }).waitFor()
              await capture(page, { route: '/jobs#new-job', theme, width, filename: `${name}-new-job-dialog.png`, extra: true, overlay: true }, false)
              await page.keyboard.press('Escape')
            }
          }
        }
      } catch (error) { report.failures.push(`${name}: ${error.message}`) }
    }
    for (const tab of ['notifications', 'access', 'integrations', 'vision', 'branding', 'license', 'system', 'logs']) {
      try {
        await gotoReady(page, app.url + '/settings?tab=' + tab, theme)
        await page.evaluate(() => document.fonts.ready)
        const filename = `${theme}-${width}-settings-${tab}.png`
        await capture(page, { route: '/settings?tab=' + tab, theme, width, filename })
      } catch (error) { report.failures.push(`${theme}-${width}-settings-${tab}: ${error.message}`) }
    }
    // Display only: this token is deliberately invalid; no reset is submitted.
    await gotoReady(page, app.url + '/reset-password?token=invalid-review-token', theme)
    const resetFile = `${theme}-${width}-reset-form.png`
    await capture(page, { route: '/reset-password?token=invalid-review-token', theme, width, filename: resetFile })
    for (const [route, opener, title] of [
      ['/projects', 'New', 'New Project'],
    ]) {
      try {
        await gotoReady(page, app.url + route, theme)
        await page.getByRole('button', { name: opener, exact: true }).waitFor({ state: 'visible' })
        await page.getByRole('button', { name: opener, exact: true }).click()
        await page.getByRole('dialog', { name: title, exact: true }).waitFor()
        const dialogFile = `${theme}-${width}-project-dialog.png`
        await capture(page, { route: '/projects#new-project', theme, width, filename: dialogFile, overlay: true }, false)
        if (theme === 'foundry') {
          const result = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze()
          report.axe.push({ route: route + '#new-project', width, violations: result.violations })
          if (result.violations.length) report.failures.push(`${theme}-${width}-project-dialog: ${result.violations.length} axe violations`)
        }
      } catch (error) { report.failures.push(`${theme}-${width}-project-dialog: ${error.message}`) }
    }
    await context.close()
  }
  if (baseline) {
    // Decode both PNGs through the browser canvas: compare pixels, not PNG metadata.
    const page = await browser.newPage()
    await page.addScriptTag({ content: `window.comparePixels = ${comparePixels.toString()}` })
    // Missing *and extra* scroll captures fail, even when all common PNGs match.
    const reference = JSON.parse(await readFile(path.join(baseline, 'report.json'), 'utf8'))
    const currentNames = new Set(report.screenshots.filter(s => s.theme === 'classic' && !s.extra).map(s => s.filename))
    for (const referenceCapture of reference.screenshots.filter(s => s.theme === 'classic' && !s.extra)) {
      if (!currentNames.has(referenceCapture.filename)) report.failures.push(`${referenceCapture.filename}: baseline capture missing from current run`)
    }
    for (const capture of report.screenshots.filter(s => s.theme === 'classic' && !s.extra)) {
      try {
        const referenceCapture = reference.screenshots.find(s => s.filename === capture.filename)
        report.comparisonMasks.push({ filename: capture.filename, actual: capture.comparisonMasks, baseline: referenceCapture?.comparisonMasks ?? null })
        const masks = comparisonMaskRectangles(capture.comparisonMasks, referenceCapture?.comparisonMasks)
        const images = await Promise.all([output, baseline].map(async dir => 'data:image/png;base64,' + (await readFile(path.join(dir, capture.filename))).toString('base64')))
        const comparison = await page.evaluate(async ({ images, masks }) => {
          const pixels = await Promise.all(images.map(src => new Promise((resolve, reject) => {
            const img = new Image(); img.onerror = reject; img.onload = () => { const c = document.createElement('canvas'); c.width = img.width; c.height = img.height; const ctx = c.getContext('2d'); ctx.drawImage(img, 0, 0); resolve({ width: c.width, height: c.height, data: ctx.getImageData(0, 0, c.width, c.height).data }) }; img.src = src
          })))
          const result = window.comparePixels(pixels[0], pixels[1], 10, masks)
          if (!result.changedPixels) return { changedPixels: 0 }
          const canvas = document.createElement('canvas'); canvas.width = result.width; canvas.height = result.height
          canvas.getContext('2d').putImageData(new ImageData(result.diff, result.width, result.height), 0, 0)
          return { changedPixels: result.changedPixels, boundingBox: result.boundingBox, actualSize: [pixels[0].width, pixels[0].height], baselineSize: [pixels[1].width, pixels[1].height], png: canvas.toDataURL('image/png') }
        }, { images, masks })
        if (comparison.changedPixels) {
          const diffImage = capture.filename.replace(/\.png$/, '-diff.png')
          const { png, ...details } = comparison
          await writeFile(path.join(output, diffImage), Buffer.from(png.split(',')[1], 'base64'))
          const referenceNoise = volatileCaptures.get(capture.filename)
          if (volatilePass(details, referenceNoise)) {
            report.volatilePasses.push({ filename: capture.filename, diffImage, ...details,
              allowedBoundingBox: { x: referenceNoise.boundingBox.x - 4, y: referenceNoise.boundingBox.y - 4,
                width: referenceNoise.boundingBox.width + 8, height: referenceNoise.boundingBox.height + 8 },
              maxChangedPixels: referenceNoise.changedPixels * 2 })
          } else {
            report.classicDiffs.push({ filename: capture.filename, diffImage, ...details })
            report.failures.push(`${capture.filename}: Classic pixels differ from baseline (>${report.pixelTolerance} per channel)`)
          }
        }
      } catch (error) { report.failures.push(`${capture.filename}: baseline unavailable: ${error.message}`) }
    }
    report.classicComparison = report.failures.some(f => /Classic pixels|baseline unavailable|baseline capture missing/.test(f)) ? 'failed' : 'passed'
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
  report.phoneSummary = { failOnFindings: phoneFail, ...summarizePhoneLayout(report.phoneLayout) }
  await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2))
}
console.log(`Saved ${report.screenshots.length} screenshots; ${report.failures.length} failures. ${path.join(output, 'report.json')}`)
if (report.phoneSummary?.captures) console.log(`Phone layout: ${report.phoneSummary.withFindings} of ${report.phoneSummary.captures} captures have findings (${phoneFail ? 'failing the run' : 'report-only; pass --phone-fail to fail'}). ${JSON.stringify(report.phoneSummary.byTheme)}`)
process.exitCode = report.failures.length ? 1 : 0
