// Cert-B live browser scenarios against the real ODIN container (see
// tests/test_contracts/test_cert_B_live_harness.py). Real Google Chrome (headless),
// real built bundle, real backend, real go2rtc WebRTC. Nothing under /api is mocked.
// Usage: node live.playwright.mjs <scenario> '<json args>'   (env CERTB_BASE, CERTB_PASSWORD, CERTB_SCREENS)
import { createRequire } from 'module'
import fs from 'fs'
import path from 'path'

// Run with cwd = <repo>/frontend (the harness does), so Playwright resolves from the app's node_modules.
const require = createRequire(path.join(process.cwd(), 'package.json'))
const { chromium } = require('@playwright/test')

const BASE = process.env.CERTB_BASE
const PASSWORD = process.env.CERTB_PASSWORD
const SCREENS = process.env.CERTB_SCREENS
const [scenario, rawArgs] = process.argv.slice(2)
const args = JSON.parse(rawArgs || '{}')
const out = { scenario, console: [], requests: [] }

const sleep = ms => new Promise(r => setTimeout(r, ms))

async function context(browser, { user, viewport = { width: 1280, height: 800 }, extra = {} } = {}) {
  const ctx = await browser.newContext({ viewport, timezoneId: 'UTC', locale: 'en-US', acceptDownloads: true, ...extra })
  if (user) {
    const r = await ctx.request.post(BASE + '/api/auth/login', { form: { username: user, password: PASSWORD } })
    if (r.status() !== 200) throw new Error('login failed ' + r.status())
  }
  return ctx
}

function watch(page) {
  page.on('console', m => { if (['error', 'warning'].includes(m.type())) out.console.push(`${m.type()}: ${m.text().slice(0, 300)}`) })
  page.on('request', r => { if (r.url().includes('/api/')) out.requests.push({ t: Date.now(), method: r.method(), url: r.url().replace(BASE, '') }) })
}

// A <video> is live when frames decode (videoWidth>0) and playback time advances.
async function videoState(locator) {
  return locator.evaluate(v => ({ w: v.videoWidth, h: v.videoHeight, t: v.currentTime, rs: v.readyState, paused: v.paused, hasStream: !!v.srcObject }))
}
async function waitLive(locator, timeout = 45000) {
  const end = Date.now() + timeout
  let first = null, last = null
  while (Date.now() < end) {
    last = await videoState(locator).catch(() => null)
    if (last && last.w > 0 && last.t > 0.3) {
      if (!first) first = last
      else if (last.t > first.t + 0.5) return { live: true, ...last, advancedBy: +(last.t - first.t).toFixed(2) }
    }
    await sleep(500)
  }
  return { live: false, ...(last || {}) }
}
const box = async l => { const b = await l.boundingBox(); return b && { x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width), h: Math.round(b.height) } }
async function shot(page, name) { const p = path.join(SCREENS, name + '.png'); await page.screenshot({ path: p }); return p }

const scenarios = {
  async camera_grid(browser) {
    const ctx = await context(browser, { user: args.user }); const page = await ctx.newPage(); watch(page)
    await page.goto(BASE + '/cameras')
    for (const name of args.names) await page.getByRole('link', { name, exact: true }).first().waitFor({ timeout: 20000 })
    out.cards = {}
    for (const name of args.names) {
      const card = page.locator('div.group', { has: page.getByRole('link', { name, exact: true }) }).last()
      out.cards[name] = { href: await page.getByRole('link', { name, exact: true }).first().getAttribute('href'), ...(await waitLive(card.locator('video'))) }
      out.cards[name].statusText = (await card.locator('span.capitalize').textContent()).trim()
      out.cards[name].liveLabel = await card.getByText('LIVE', { exact: true }).isVisible()
    }
    out.videoCount = await page.locator('main video').count()
    out.subtitle = await page.getByText(/cameras? available/).first().textContent()
    out.screenshot = await shot(page, 'camera-grid')
    await ctx.close()
  },

  async camera_toggle(browser) {
    const ctx = await context(browser, { user: args.user }); const page = await ctx.newPage(); watch(page)
    await page.goto(BASE + '/cameras')
    await page.getByText(/cameras? available/).first().waitFor()
    await page.locator('button[title="Camera Settings"]').click()
    await page.getByRole('heading', { name: 'Camera Settings' }).waitFor()
    await page.waitForTimeout(1500)
    out.settingsNames = await page.locator('div.py-2.px-3 span.text-sm').allTextContents()
    if (args.name) {
      const row = page.locator('div.py-2.px-3', { has: page.getByText(args.name, { exact: true }) }).first()
      const button = row.getByRole('button')
      out.before = (await button.textContent()).trim()
      await button.click()
      await page.waitForFunction(([n, b]) => [...document.querySelectorAll('span')].some(s => s.textContent === n) && true, [args.name, out.before])
      await page.waitForTimeout(1500)
      out.after = (await button.textContent()).trim()
      // The grid follows the refetched camera list; allow for a loaded host instead of a fixed sleep.
      const gridLink = page.locator('main a[href^="/cameras/"]', { hasText: args.name })
      const expected = out.after === 'Disabled' ? 0 : 1
      const until = Date.now() + 20000
      do { await page.waitForTimeout(500); out.gridHasAfterToggle = await gridLink.count() } while (out.gridHasAfterToggle !== expected && Date.now() < until)
      await page.reload(); await page.getByText(/cameras? available/).first().waitFor(); await page.waitForTimeout(2000)
      out.gridHasAfterReload = await page.locator('main a[href^="/cameras/"]', { hasText: args.name }).count()
      out.gridNames = await page.locator('main div.group a.font-medium').allTextContents()
    }
    out.screenshot = await shot(page, 'camera-toggle-' + (args.name ? args.name.replace(/\W+/g, '-') : 'panel') + '-' + (out.after || 'list'))
    await ctx.close()
  },

  async control_room(browser) {
    const ctx = await context(browser, { user: args.user }); const page = await ctx.newPage(); watch(page)
    await page.goto(BASE + '/cameras')
    for (const name of args.names) await page.getByRole('link', { name, exact: true }).first().waitFor()
    await page.locator('body').click({ position: { x: 640, y: 780 } })
    await page.keyboard.press('f')
    const overlay = page.locator('div.fixed.inset-0.z-50.bg-black')
    await overlay.waitFor()
    out.overlayBox = await box(overlay)
    out.viewport = page.viewportSize()
    out.rootHasControlRoomClass = await page.evaluate(() => document.documentElement.classList.contains('control-room-mode'))
    // What the user sees at the sidebar's position is the overlay, not the sidebar.
    out.topElementAtSidebarIsOverlay = await page.evaluate(() => !!document.elementFromPoint(100, 300)?.closest('div.fixed.inset-0.z-50'))
    out.overlayNames = await overlay.locator('span.font-mono.text-white').allTextContents()
    const videos = overlay.locator('video'); out.overlayVideoCount = await videos.count()
    out.overlayVideos = []
    for (let i = 0; i < out.overlayVideoCount; i++) out.overlayVideos.push(await waitLive(videos.nth(i)))
    out.screenshot = await shot(page, 'control-room')
    await page.keyboard.press('Escape'); await page.waitForTimeout(500)
    out.overlayAfterEscape = await overlay.count()
    out.rootClassAfterEscape = await page.evaluate(() => document.documentElement.classList.contains('control-room-mode'))
    out.gridVideosAfterEscape = await page.locator('main video').count()
    await ctx.close()
  },

  async pip(browser) {
    const ctx = await context(browser, { user: args.user }); const page = await ctx.newPage(); watch(page)
    await page.goto(BASE + '/cameras')
    const link = page.getByRole('link', { name: args.name, exact: true }).first(); await link.waitFor()
    const card = page.locator('div.group', { has: link }).last()
    await card.hover(); await card.locator('button[title="Picture-in-Picture"]').click()
    const pip = page.locator('section[aria-label="Picture-in-picture camera"]'); await pip.waitFor()
    out.title = (await pip.locator('header span').textContent()).trim()
    out.live1 = await waitLive(pip.locator('video'))
    out.status1 = (await pip.getByRole('status').textContent()).trim()
    out.box1 = await box(pip)
    const header = pip.locator('header'); const hb = await header.boundingBox()
    await page.mouse.move(hb.x + 30, hb.y + 10); await page.mouse.down()
    await page.mouse.move(hb.x - 200, hb.y - 250, { steps: 12 }); await page.mouse.up()
    out.box2 = await box(pip)
    await pip.getByRole('button', { name: 'Resize camera player' }).click(); out.box3 = await box(pip)
    await pip.getByRole('button', { name: 'Resize camera player' }).click(); out.box4 = await box(pip)
    const t0 = (await videoState(pip.locator('video'))).t
    await page.getByRole('link', { name: 'Printers', exact: true }).first().click()
    await page.waitForURL(u => u.pathname === '/printers'); await page.waitForTimeout(2500)
    out.urlAfterNav = new URL(page.url()).pathname
    out.visibleAfterNav = await pip.isVisible()
    out.boxAfterNav = await box(pip)
    out.liveAfterNav = await waitLive(pip.locator('video'))
    out.timeAdvancedAcrossNav = out.liveAfterNav.t > t0
    await page.getByRole('link', { name: 'Archives', exact: true }).first().click()
    await page.waitForURL(u => u.pathname === '/archives'); await page.waitForTimeout(1500)
    out.visibleAfterSecondNav = await pip.isVisible()
    out.liveAfterSecondNav = await waitLive(pip.locator('video'))
    out.screenshot = await shot(page, 'pip-after-navigation')
    await pip.getByRole('button', { name: 'Close camera player' }).click(); out.countAfterClose = await pip.count()
    await ctx.close()
  },

  async camera_detail(browser) {
    const vp = args.mobile ? { width: 375, height: 812 } : { width: 1280, height: 800 }
    const extra = args.mobile ? { isMobile: true, hasTouch: true, deviceScaleFactor: 2 } : {}
    const ctx = await context(browser, { user: args.user, viewport: vp, extra }); const page = await ctx.newPage(); watch(page)
    await page.goto(BASE + `/cameras/${args.id}`)
    await page.getByRole('heading', { name: args.name }).waitFor({ timeout: 20000 })
    const stream = page.locator('main div.flex-\\[7\\]'), panels = page.locator('main div.flex-\\[3\\]')
    out.live = await waitLive(stream.locator('video'))
    await page.waitForTimeout(args.settleMs || 4000)
    out.stream = await box(stream); out.panels = await box(panels)
    out.row = await box(stream.locator('xpath=..'))
    out.flexDirection = await stream.evaluate(e => getComputedStyle(e.parentElement).flexDirection)
    out.docScrollWidth = await page.evaluate(() => document.documentElement.scrollWidth)
    out.panelTitles = await panels.locator('h3').allTextContents()
    out.panelText = (await panels.innerText()).split('\n').map(s => s.trim()).filter(Boolean)
    out.progressBar = await panels.locator('[role="progressbar"]').count()
    out.aiBadge = await page.locator('[title="Vigil AI active"]').isVisible()
    out.screenshot = await shot(page, 'camera-detail-' + (args.mobile ? 'mobile-375' : 'desktop-1280') + (args.tag ? '-' + args.tag : ''))
    if (!args.mobile && args.fullscreen) {
      await page.locator('button[title="Toggle fullscreen"]').click(); await page.waitForTimeout(800)
      out.fullscreenElementIsStream = await page.evaluate(() => !!document.fullscreenElement && document.fullscreenElement.classList.contains('flex-[7]'))
      out.fullscreenBox = await box(stream)
      await page.evaluate(() => document.exitFullscreen())
      await page.waitForTimeout(800)
      out.fullscreenAfterExit = await page.evaluate(() => !!document.fullscreenElement)
    }
    if (!args.mobile && args.breadcrumb) {
      await page.getByRole('link', { name: 'Cameras', exact: true }).last().click()
      await page.waitForURL(u => u.pathname === '/cameras'); out.breadcrumbUrl = new URL(page.url()).pathname
      out.breadcrumbLandsOnGrid = await page.getByText(/cameras? available/).first().waitFor({ state: 'visible', timeout: 15000 }).then(() => true, () => false)
    }
    await ctx.close()
  },

  async tv(browser) {
    const ctx = await context(browser, { user: args.user, viewport: { width: 1920, height: 1080 } }); const page = await ctx.newPage(); watch(page)
    await page.goto(BASE + '/cameras'); await page.getByText(/cameras? available/).first().waitFor()
    const tvLink = page.locator('a[title="Open TV Dashboard in new tab"]')
    out.tvLink = { href: await tvLink.getAttribute('href'), target: await tvLink.getAttribute('target'), rel: await tvLink.getAttribute('rel') }
    const [tv] = await Promise.all([ctx.waitForEvent('page'), tvLink.click()])
    watch(tv); await tv.waitForLoadState()
    out.newTabUrl = new URL(tv.url()).pathname
    out.originalTabUrl = new URL(page.url()).pathname
    await tv.locator('[data-testid="tv-printer-card"]').first().waitFor({ timeout: 20000 })
    const firstCardAt = Date.now()
    await tv.waitForTimeout(3000)
    const root = tv.locator('div.fixed.inset-0').first()
    out.rootBox = await box(root); out.viewport = tv.viewportSize()
    out.sidebarLinks = await tv.locator('a[href="/printers"], a[href="/archives"], nav a').count()
    const header = root.locator(':scope > div').first()
    out.brand = (await header.locator('h1').textContent()).trim()
    out.headerText = (await header.innerText()).split('\n').map(s => s.trim()).filter(Boolean)
    out.nowUtc = new Date().toISOString()
    out.cardsPage1 = await tv.locator('[data-testid="tv-printer-card"]').evaluateAll(cs => cs.map(c => c.getAttribute('data-printer-name')))
    out.cards = {}
    for (const name of args.inspect || []) {
      const card = tv.locator(`[data-testid="tv-printer-card"][data-printer-name="${name}"]`)
      if (!(await card.count())) { out.cards[name] = null; continue }
      const v = card.locator('video')
      out.cards[name] = {
        status: (await card.locator('[data-testid="tv-printer-status"]').textContent()).trim(),
        text: (await card.innerText()).split('\n').map(s => s.trim()).filter(Boolean),
        hasVideo: await v.count(), video: (await v.count()) ? await waitLive(v) : null,
      }
    }
    const statsBar = root.locator(':scope > div').last()
    out.statsText = (await statsBar.innerText()).split('\n').map(s => s.trim()).filter(Boolean)
    out.dots = await statsBar.locator('div.w-1\\.5.h-1\\.5.rounded-full').count()
    out.activeDot = await statsBar.locator('div.w-1\\.5.h-1\\.5.rounded-full').evaluateAll(ds => ds.findIndex(d => d.className.includes('brand-primary')))
    out.screenshotPage1 = await shot(tv, 'tv-page1')
    await tv.waitForFunction(first => document.querySelector('[data-testid="tv-printer-card"]')?.getAttribute('data-printer-name') !== first, out.cardsPage1[0], { timeout: 45000 })
    out.pageTurnAfterMs = Date.now() - firstCardAt
    out.cardsPage2 = await tv.locator('[data-testid="tv-printer-card"]').evaluateAll(cs => cs.map(c => c.getAttribute('data-printer-name')))
    out.activeDotPage2 = await statsBar.locator('div.w-1\\.5.h-1\\.5.rounded-full').evaluateAll(ds => ds.findIndex(d => d.className.includes('brand-primary')))
    out.screenshotPage2 = await shot(tv, 'tv-page2')
    await tv.keyboard.press('Escape'); await tv.waitForURL(u => u.pathname === '/'); out.afterEscape = new URL(tv.url()).pathname
    await tv.goto(BASE + '/tv'); await tv.locator('button[title="Exit TV Mode (Esc)"]').click(); await tv.waitForURL(u => u.pathname === '/'); out.afterX = new URL(tv.url()).pathname
    await ctx.close()
  },

  async overlay(browser) {
    const ctx = await context(browser, { viewport: { width: 1920, height: 1080 } }); const page = await ctx.newPage(); watch(page)
    const q = args.query || ''
    await page.goto(BASE + `/overlay/${args.id}${q}`)
    await page.locator('[data-testid="overlay-printer-name"]').waitFor({ timeout: 20000 })
    out.url = new URL(page.url()).pathname + new URL(page.url()).search
    out.cookies = (await ctx.cookies()).map(c => c.name)
    const rootEl = page.locator('#root > div').first()
    out.rootBg = await rootEl.evaluate(e => getComputedStyle(e).backgroundColor)
    out.rootBox = await box(rootEl)
    out.cameraPanel = await page.locator('div.flex-\\[7\\]').count() ? await box(page.locator('div.flex-\\[7\\]')) : null
    out.statusPanel = await box(page.locator('[data-testid="overlay-printer-name"]').locator('xpath=../..'))
    out.texts = (await page.locator('[data-testid="overlay-printer-name"]').locator('xpath=../..').innerText()).split('\n').map(s => s.trim()).filter(Boolean)
    out.progressBarWidth = await page.locator('div.h-3.rounded-full > div').evaluate(e => e.style.width).catch(() => null)
    const img = page.locator('img[alt="Camera stream"]')
    if (await img.count()) {
      // An MJPEG <img> has a size once its first frame arrived (the relay may transcode first).
      await img.evaluate(i => new Promise(resolve => {
        const end = Date.now() + 30000
        const tick = () => (i.naturalWidth > 0 || i.style.display === 'none' || Date.now() > end) ? resolve() : setTimeout(tick, 250)
        tick()
      }))
    }
    out.img = (await img.count()) ? await img.evaluate(i => ({ src: i.getAttribute('src'), naturalWidth: i.naturalWidth, naturalHeight: i.naturalHeight, complete: i.complete, display: i.style.display })) : null
    if (out.img) {
      // Headers only: the body is an endless multipart stream.
      out.imgFetch = await page.evaluate(async src => {
        const abort = new AbortController(); const r = await fetch(src, { signal: abort.signal })
        const result = { status: r.status, type: r.headers.get('content-type') }; abort.abort(); return result
      }, out.img.src)
      if (args.screenshotFrames) {
        const a = await shot(page, 'overlay-frame-a'); await page.waitForTimeout(3000); const b = await shot(page, 'overlay-frame-b')
        out.frameShots = [a, b]
      }
    }
    if (args.pollSeconds) {
      out.requests = []
      await page.waitForTimeout(args.pollSeconds * 1000)
      out.pollTimes = out.requests.filter(r => r.url === `/api/overlay/${args.id}`).map(r => r.t)
    }
    out.screenshot = await shot(page, 'overlay' + (q ? '-' + q.replace(/\W+/g, '-') : ''))
    await ctx.close()
  },

  async copy_overlay(browser) {
    const ctx = await context(browser, { user: args.user, extra: { permissions: ['clipboard-read', 'clipboard-write'] } }); const page = await ctx.newPage(); watch(page)
    await page.goto(BASE + '/printers')
    const buttons = page.getByRole('button', { name: 'Copy OBS overlay URL' })
    await buttons.first().waitFor({ timeout: 20000 })
    out.buttonCount = await buttons.count()
    const card = page.locator('div', { has: page.getByText(args.name, { exact: true }) }).filter({ has: buttons }).last()
    await page.evaluate(() => navigator.clipboard.writeText(''))
    await card.getByRole('button', { name: 'Copy OBS overlay URL' }).click()
    // The copy waits for a camera token to be minted first.
    const end = Date.now() + 15000
    do { await page.waitForTimeout(250); out.clipboard = await page.evaluate(() => navigator.clipboard.readText()) } while (!out.clipboard && Date.now() < end)
    out.tokenRequests = out.requests.filter(r => r.method === 'POST' && r.url === '/api/tokens').length
    await ctx.close()
  },

  async token_video(browser) {
    const ctx = await context(browser); const page = await ctx.newPage(); watch(page)
    await page.goto(BASE + '/login')
    out.cookies = (await ctx.cookies()).map(c => c.name)
    await page.evaluate(src => { const v = document.createElement('video'); v.id = 'certb'; v.muted = true; v.autoplay = true; v.src = src; document.body.appendChild(v) }, args.src)
    const v = page.locator('#certb')
    const end = Date.now() + 20000; let st = null
    while (Date.now() < end) { st = await v.evaluate(e => ({ w: e.videoWidth, h: e.videoHeight, d: e.duration, rs: e.readyState, err: e.error && e.error.code, t: e.currentTime })); if (st.err || (st.w > 0 && st.d > 0)) break; await sleep(300) }
    out.video = st
    await ctx.close()
  },

  async timelapse_gallery(browser) {
    const ctx = await context(browser, { user: args.user }); const page = await ctx.newPage(); watch(page)
    await page.goto(BASE + '/timelapses')
    await page.getByRole('heading', { name: 'Timelapses' }).waitFor()
    await page.locator('article').first().waitFor({ timeout: 20000 })
    out.articles = await page.locator('article').evaluateAll(as => as.map(a => a.innerText.split('\n').map(s => s.trim()).filter(Boolean)))
    out.gridColumns = await page.locator('article').first().evaluate(a => getComputedStyle(a.parentElement).gridTemplateColumns.split(' ').length)
    out.pagination = (await page.getByText(/^Page \d+ of \d+$/).textContent()).trim()
    out.buttons = await page.locator('article button').allTextContents()
    if (args.filterPrinter) {
      out.requests = []
      await page.getByLabel('Printer filter').selectOption(String(args.filterPrinter)); await page.waitForTimeout(1500)
      out.afterPrinterFilter = await page.locator('article h2').allTextContents()
      await page.getByLabel('Status filter').selectOption(args.filterStatus || 'ready'); await page.waitForTimeout(1500)
      out.afterStatusFilter = await page.locator('article').evaluateAll(as => as.map(a => a.querySelector('p')?.textContent))
      out.filterRequests = out.requests.filter(r => r.url.startsWith('/api/timelapses?')).map(r => r.url)
      await page.getByLabel('Status filter').selectOption(''); await page.getByLabel('Printer filter').selectOption(''); await page.waitForTimeout(1000)
    }
    if (args.play) {
      await page.getByRole('button', { name: `Play recording ${args.play}` }).click()
      const player = page.locator('section[aria-label="Recording player"]'); await player.waitFor()
      out.playerIsDialog = await player.evaluate(p => p.getAttribute('role') === 'dialog' || !!p.closest('[role="dialog"]') || getComputedStyle(p).position === 'fixed')
      const v = player.locator('video')
      const end = Date.now() + 20000; let st = null
      while (Date.now() < end) { st = await v.evaluate(e => ({ w: e.videoWidth, h: e.videoHeight, d: e.duration, rs: e.readyState, t: e.currentTime, src: e.currentSrc.replace(location.origin, '') })); if (st.w > 0 && st.d > 0 && st.t >= Math.min(0.2, st.d) - 0.001) break; await sleep(300) }
      out.player = st
      out.screenshotPlayer = await shot(page, 'timelapse-player')
      await player.getByRole('button', { name: 'Close player' }).click()
    }
    if (args.download) {
      const [dl] = await Promise.all([page.waitForEvent('download'), page.getByRole('button', { name: `Download recording ${args.download}` }).click()])
      const p = path.join(SCREENS, '..', 'work', `download-${args.download}.mp4`); await dl.saveAs(p)
      out.download = { suggested: dl.suggestedFilename(), path: p, bytes: fs.statSync(p).size, url: dl.url().replace(BASE, '') }
    }
    await ctx.close()
  },

  async timelapse_editor(browser) {
    const ctx = await context(browser, { user: args.user }); const page = await ctx.newPage(); watch(page)
    await page.goto(BASE + '/timelapses'); await page.locator('article').first().waitFor({ timeout: 20000 })
    out.editButtons = await page.getByRole('button', { name: /^Edit recording/ }).count()
    if (!args.id) { await ctx.close(); return }
    await page.getByRole('button', { name: `Edit recording ${args.id}` }).click()
    const dialog = page.getByRole('dialog', { name: 'Edit recording' }); await dialog.waitFor()
    out.dialogModal = await dialog.getAttribute('aria-modal')
    const v = dialog.locator('video').first(); await v.waitFor({ timeout: 20000 })
    const end = Date.now() + 20000; let st = null
    while (Date.now() < end) { st = await v.evaluate(e => ({ w: e.videoWidth, d: e.duration, rs: e.readyState })); if (st.w > 0 && st.d > 0) break; await sleep(300) }
    out.preview = st
    out.sliders = await dialog.locator('input[type="range"]').evaluateAll(rs => rs.map(r => ({ label: r.getAttribute('aria-label'), min: r.min, max: r.max, value: r.value })))
    out.speedOptions = await dialog.locator('select option').evaluateAll(os => os.map(o => o.textContent))
    const setRange = async (label, value) => dialog.locator(`input[aria-label="${label}"]`).evaluate((el, val) => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set; setter.call(el, String(val)); el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true }))
    }, value)
    await setRange('Trim start', args.trimStart); await setRange('Trim end', args.trimEnd)
    out.numbersAfterSlider = await dialog.locator('input[type="number"]').evaluateAll(ns => ns.map(n => n.value))
    out.editRequests = []
    page.on('request', r => { if (/\/(trim|speed)$/.test(r.url())) out.editRequests.push({ url: r.url().replace(BASE, ''), body: r.postData() }) })
    await dialog.getByRole('button', { name: 'Confirm trim' }).click()
    await dialog.getByText(/^Edit completed:/).waitFor({ timeout: 180000 })
    out.trimReceipt = (await dialog.getByText(/^Edit completed:/).textContent()).trim()
    const edited = dialog.locator('video').last()
    const e2 = Date.now() + 20000; let es = null
    while (Date.now() < e2) { es = await edited.evaluate(e => ({ w: e.videoWidth, d: e.duration, rs: e.readyState, src: e.currentSrc.replace(location.origin, '') })); if (es.w > 0 && es.d > 0) break; await sleep(300) }
    out.trimVideo = es
    out.trimDownloadHref = await dialog.getByRole('link', { name: 'Download edited recording' }).getAttribute('href')
    out.screenshotTrim = await shot(page, 'timelapse-editor-trim')
    if (args.speed) {
      await dialog.getByRole('button', { name: 'Close editor' }).click()
      await page.reload(); await page.locator('article').first().waitFor({ timeout: 20000 })
      const target = args.speedId || args.id
      await page.getByRole('button', { name: `Edit recording ${target}` }).click()
      const d2 = page.getByRole('dialog', { name: 'Edit recording' }); await d2.waitFor()
      await d2.locator('video').first().waitFor({ timeout: 20000 }); await page.waitForTimeout(1500)
      await d2.locator('select').selectOption(String(args.speed))
      await d2.getByRole('button', { name: 'Create speed recording' }).click()
      await d2.getByText(/^Edit completed:/).waitFor({ timeout: 180000 })
      out.speedReceipt = (await d2.getByText(/^Edit completed:/).textContent()).trim()
      out.speedDownloadHref = await d2.getByRole('link', { name: 'Download edited recording' }).getAttribute('href').catch(() => null)
      out.screenshotSpeed = await shot(page, 'timelapse-editor-speed')
    }
    await ctx.close()
  },

  async timelapse_delete(browser) {
    const ctx = await context(browser, { user: args.user }); const page = await ctx.newPage(); watch(page)
    await page.goto(BASE + '/timelapses'); await page.locator('article').first().waitFor({ timeout: 20000 })
    out.deleteButtons = await page.getByRole('button', { name: /^Delete recording/ }).allTextContents()
    if (args.id) {
      await page.getByRole('button', { name: `Delete recording ${args.id}` }).click()
      await page.getByRole('button', { name: 'Confirm deletion' }).click()
      await page.waitForFunction(id => ![...document.querySelectorAll('article p')].some(p => p.textContent.startsWith(`Recording ${id}:`)), args.id, { timeout: 60000 })
      out.deleted = true
      out.deleteRequests = out.requests.filter(r => r.method === 'DELETE')
    }
    await ctx.close()
  },

  async archives(browser) {
    const ctx = await context(browser, { user: args.user, viewport: { width: 1440, height: 900 } }); const page = await ctx.newPage(); watch(page)
    await page.goto(BASE + '/archives')
    await page.locator('table tbody tr').first().waitFor({ timeout: 20000 })
    out.headers = await page.locator('table thead th').allTextContents()
    out.rowCount = await page.locator('table tbody tr').count()
    out.firstRow = await page.locator('table tbody tr').first().evaluate(tr => ({ cells: [...tr.cells].map(c => c.innerText.trim()), img: !!tr.querySelector('img'), badge: !!tr.cells[4]?.querySelector('span') }))
    out.pager = await page.getByText(/^\d+ \/ \d+$/).textContent().catch(() => null)
    out.requests = []
    const nextButton = page.locator('button:has(svg.lucide-chevron-right)').last()
    if (await nextButton.count()) { await nextButton.click(); await page.waitForTimeout(1500); out.page2Rows = await page.locator('table tbody tr').count(); out.pagerAfter = await page.getByText(/^\d+ \/ \d+$/).textContent() }
    out.pageRequests = out.requests.filter(r => r.url.startsWith('/api/archives?')).map(r => r.url)
    await page.goto(BASE + '/archives'); await page.locator('table tbody tr').first().waitFor()
    out.requests = []
    if (args.search) {
      await page.getByPlaceholder('Search prints...').fill(args.search); await page.getByPlaceholder('Search prints...').press('Enter'); await page.waitForTimeout(1500)
      out.afterSearch = await page.locator('table tbody tr td:nth-child(3) div.font-medium').allTextContents()
      await page.getByPlaceholder('Search prints...').fill(''); await page.getByPlaceholder('Search prints...').press('Enter'); await page.waitForTimeout(800)
    }
    if (args.printer) {
      await page.locator('select').nth(0).selectOption(String(args.printer)); await page.waitForTimeout(1500)
      out.afterPrinter = await page.locator('table tbody tr td:nth-child(4)').allTextContents()
      await page.locator('select').nth(0).selectOption(''); await page.waitForTimeout(800)
    }
    if (args.status) {
      await page.locator('select').nth(1).selectOption(args.status); await page.waitForTimeout(1500)
      out.afterStatus = await page.locator('table tbody tr td:nth-child(5)').allTextContents()
      await page.locator('select').nth(1).selectOption(''); await page.waitForTimeout(800)
    }
    out.filterRequests = out.requests.filter(r => r.url.startsWith('/api/archives?')).map(r => r.url)
    if (args.tag) {
      await page.locator('table tbody tr button', { hasText: args.tag }).first().click(); await page.waitForTimeout(1500)
      out.afterTag = await page.locator('table tbody tr td:nth-child(3) div.font-medium').allTextContents()
      out.urlAfterTag = new URL(page.url()).search
      await page.goto(BASE + '/archives'); await page.locator('table tbody tr').first().waitFor()
    }
    if (args.compare) {
      for (const name of args.compare) await page.locator('table tbody tr', { hasText: name }).first().locator('input[type="checkbox"]').check()
      await page.getByRole('button', { name: 'Compare' }).click()
      const dialog = page.getByRole('dialog'); await dialog.getByText('Compare Archives').waitFor(); await page.waitForTimeout(1500)
      out.compareRows = await dialog.locator('tbody tr').evaluateAll(trs => trs.map(tr => ({ cells: [...tr.cells].map(c => c.innerText.trim()), highlighted: tr.className.includes('bg-amber') })))
      out.compareHeads = await dialog.locator('thead th').allTextContents()
      out.screenshotCompare = await shot(page, 'archive-compare')
      await page.keyboard.press('Escape'); await page.waitForTimeout(500)
    }
    if (args.detail) {
      await page.goto(BASE + '/archives'); await page.locator('table tbody tr').first().waitFor()
      await page.locator('table tbody tr', { hasText: args.detail }).first().locator('td').nth(2).click()
      const dialog = page.getByRole('dialog'); await dialog.waitFor(); await page.waitForTimeout(800)
      out.detailText = (await dialog.innerText()).split('\n').map(s => s.trim()).filter(Boolean)
      out.detailHasNotes = await dialog.getByPlaceholder('Add notes...').count()
      out.detailHasDelete = await dialog.getByRole('button', { name: /Delete/ }).count()
      out.detailHas3D = await dialog.getByRole('button', { name: /View 3D Model/ }).count()
      out.detailHasReprint = await dialog.getByRole('button', { name: /^Reprint$/ }).count()
      out.detailHasTagEditor = await dialog.getByPlaceholder('Add tag...').count()
      if (args.notes && out.detailHasNotes) {
        await dialog.getByPlaceholder('Add notes...').fill(args.notes)
        out.notesRequests = []
        await dialog.getByRole('button', { name: /Save/ }).first().click(); await page.waitForTimeout(1500)
        out.notesPatch = out.requests.filter(r => r.method === 'PATCH').map(r => r.url)
      }
      if (args.addTag && out.detailHasTagEditor) {
        await dialog.getByPlaceholder('Add tag...').fill(args.addTag); await dialog.getByPlaceholder('Add tag...').press('Enter'); await page.waitForTimeout(1500)
        out.tagPatch = out.requests.filter(r => r.method === 'PATCH' && r.url.endsWith('/tags')).map(r => r.url)
      }
      out.screenshotDetail = await shot(page, 'archive-detail-' + args.user)
      if (args.reprint && out.detailHasReprint) {
        await dialog.getByRole('button', { name: /^Reprint$/ }).click(); await page.waitForTimeout(800)
        out.reprintText = (await dialog.innerText()).split('\n').map(s => s.trim()).filter(Boolean)
        out.reprintAmsRequests = out.requests.filter(r => r.url.includes('ams-preview')).map(r => r.url)
        await dialog.locator('select').last().selectOption(String(args.reprint.printer)); await page.waitForTimeout(1500)
        out.reprintAmsRequestsAfterPrinter = out.requests.filter(r => r.url.includes('ams-preview')).map(r => r.url)
        out.reprintTextAfterPrinter = (await dialog.innerText()).split('\n').map(s => s.trim()).filter(Boolean)
        out.plateButtons = await dialog.locator('button', { hasText: /^(Plate )?\d+$/ }).allTextContents()
        if (args.reprint.plate != null && out.plateButtons.length) await dialog.locator('button', { hasText: /^(Plate )?\d+$/ }).nth(args.reprint.plate).click()
        out.screenshotReprint = await shot(page, 'archive-reprint')
        const reprintButtons = dialog.getByRole('button', { name: /^Reprint$/ })
        await reprintButtons.last().click(); await page.waitForTimeout(2500)
        out.reprintPost = out.requests.filter(r => r.method === 'POST' && r.url.includes('/reprint')).map(r => r.url)
      }
      if (args.deleteDetail && out.detailHasDelete) {
        page.once('dialog', d => d.accept())
        await dialog.getByRole('button', { name: /Delete/ }).first().click(); await page.waitForTimeout(800)
        const confirm = page.getByRole('button', { name: /^(Delete|Confirm)/ }).last()
        if (await confirm.isVisible().catch(() => false)) await confirm.click()
        await page.waitForTimeout(2000)
        out.deleteRequests = out.requests.filter(r => r.method === 'DELETE').map(r => r.url)
      }
    }
    await ctx.close()
  },

  async viewer3d(browser) {
    const ctx = await context(browser, { user: args.user, viewport: { width: 1440, height: 900 } }); const page = await ctx.newPage(); watch(page)
    if (args.from === 'archive') {
      await page.goto(BASE + '/archives'); await page.locator('table tbody tr').first().waitFor({ timeout: 20000 })
      await page.locator('table tbody tr', { hasText: args.name }).first().locator('td').nth(2).click()
      await page.getByRole('button', { name: /View 3D Model/ }).click()
    } else {
      await page.goto(BASE + '/models'); await page.getByRole('heading', { name: args.name }).first().waitFor({ timeout: 20000 })
      const card = page.locator('div', { has: page.getByRole('heading', { name: args.name }) }).filter({ has: page.getByRole('button', { name: 'View 3D preview' }) }).last()
      out.previewButtons = await page.getByRole('button', { name: 'View 3D preview' }).count()
      await card.getByRole('button', { name: 'View 3D preview' }).click()
    }
    const info = page.getByText(/\d[\d,]* vertices · \d[\d,]* triangles/); await info.waitFor({ timeout: 30000 })
    out.meshInfo = (await info.textContent()).trim()
    out.meshRequests = out.requests.filter(r => r.url.includes('/mesh')).map(r => r.url)
    const canvas = page.locator('canvas').last(); await page.waitForTimeout(1500)
    out.webgl = await canvas.evaluate(c => { const g = c.getContext('webgl2') || c.getContext('webgl'); return { w: c.width, h: c.height, hasContext: !!g } })
    const cb = await canvas.boundingBox(); out.canvasBox = cb
    const snap = async name => { const p = path.join(SCREENS, `viewer3d-${args.from}-${name}.png`); await canvas.screenshot({ path: p }); return p }
    out.shots = { initial: await snap('initial') }
    const cx = cb.x + cb.width / 2, cy = cb.y + cb.height / 2
    await page.mouse.move(cx, cy); await page.mouse.down({ button: 'left' }); await page.mouse.move(cx + 120, cy + 40, { steps: 10 }); await page.mouse.up({ button: 'left' }); await page.waitForTimeout(400)
    out.shots.rotated = await snap('rotated')
    await page.mouse.move(cx, cy); await page.mouse.down({ button: 'right' }); await page.mouse.move(cx + 80, cy - 60, { steps: 10 }); await page.mouse.up({ button: 'right' }); await page.waitForTimeout(400)
    out.shots.panned = await snap('panned')
    await page.mouse.move(cx, cy); for (let i = 0; i < 4; i++) { await page.mouse.wheel(0, -200); await page.waitForTimeout(80) } await page.waitForTimeout(400)
    out.shots.zoomed = await snap('zoomed')
    out.pageScrollAfterWheel = await page.evaluate(() => window.scrollY)
    const buttons = await page.locator('button').evaluateAll(bs => bs.map(b => b.getAttribute('aria-label') || b.getAttribute('title') || b.textContent.trim()).filter(Boolean))
    out.viewerButtons = buttons.filter(b => /wire|zoom|reset/i.test(b))
    await page.getByRole('button', { name: 'Reset view' }).click(); await page.waitForTimeout(400); out.shots.reset = await snap('reset')
    await page.getByRole('button', { name: 'Wireframe' }).click(); await page.waitForTimeout(400); out.shots.wireframe = await snap('wireframe')
    await page.getByRole('button', { name: 'Wireframe' }).click(); await page.getByRole('button', { name: 'Zoom in' }).click(); await page.waitForTimeout(400); out.shots.zoomIn = await snap('zoom-in')
    await page.getByRole('button', { name: 'Zoom out' }).click(); await page.getByRole('button', { name: 'Zoom out' }).click(); await page.waitForTimeout(400); out.shots.zoomOut = await snap('zoom-out')
    // Touch: one-finger rotate via CDP touch emulation.
    const cdp = await ctx.newCDPSession(page)
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: cx, y: cy }] })
    for (let i = 1; i <= 8; i++) await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: cx + i * 12, y: cy }] })
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] }); await page.waitForTimeout(400)
    out.shots.touchRotated = await snap('touch-rotated')
    await page.getByRole('button', { name: 'Reset view' }).click(); await page.waitForTimeout(300); out.shots.touchBase = await snap('touch-base')
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: cx - 40, y: cy, id: 1 }, { x: cx + 40, y: cy, id: 2 }] })
    for (let i = 1; i <= 8; i++) await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: cx - 40 - i * 12, y: cy, id: 1 }, { x: cx + 40 + i * 12, y: cy, id: 2 }] })
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] }); await page.waitForTimeout(400)
    out.shots.pinched = await snap('pinched')
    await ctx.close()
  },

  async print_log(browser) {
    const ctx = await context(browser, { user: args.user, viewport: { width: 1440, height: 900 } }); const page = await ctx.newPage(); watch(page)
    await page.goto(BASE + '/print-log'); await page.locator('table tbody tr').first().waitFor({ timeout: 20000 })
    out.headers = await page.locator('table thead th').allTextContents()
    out.rows = await page.locator('table tbody tr').evaluateAll(trs => trs.map(tr => [...tr.cells].map(c => c.innerText.trim())))
    out.requests = []
    if (args.status) { await page.locator('select').nth(args.statusSelectIndex ?? 1).selectOption(args.status); await page.waitForTimeout(1500); out.afterStatus = await page.locator('table tbody tr').evaluateAll(trs => trs.map(tr => [...tr.cells].map(c => c.innerText.trim()))) }
    const [dl] = await Promise.all([page.waitForEvent('download'), page.getByRole('button', { name: /Export CSV/ }).click()])
    const p = path.join(SCREENS, '..', 'work', 'print_log_export.csv'); await dl.saveAs(p)
    out.csvPath = p; out.csvName = dl.suggestedFilename()
    out.exportRequests = out.requests.filter(r => r.url.includes('/log/export')).map(r => r.url)
    await ctx.close()
  },
}

const browser = await chromium.launch({ channel: 'chrome', headless: true, args: ['--autoplay-policy=no-user-gesture-required', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] })
try {
  if (!scenarios[scenario]) throw new Error('unknown scenario ' + scenario)
  await scenarios[scenario](browser)
} finally {
  await browser.close()
}
console.log('CERTB_JSON=' + JSON.stringify(out))
