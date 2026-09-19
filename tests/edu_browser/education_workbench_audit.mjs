import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'

const requireFromFrontend = createRequire(new URL('../../frontend/package.json', import.meta.url))
const AxeBuilder = requireFromFrontend('@axe-core/playwright').default
const { chromium } = requireFromFrontend('@playwright/test')

const baseURL = process.env.EDU_FRONTEND_URL || 'http://127.0.0.1:4173'
const outputDir = process.env.CTEC_POC_UI_OUTPUT || 'artifacts/ctec-poc-ui'
const now = new Date().toISOString()

const center = {
  id: 1,
  org_id: 1,
  name: 'Engineering Design',
  code: 'ENG-101',
  description: 'CTEC pilot class',
  active: true,
  revision: 4,
  created_at: '2026-09-19T12:00:00Z',
  updated_at: '2026-09-19T12:00:00Z',
  counts: { active_grants: 4, active_printers: 1, submissions: 1 },
}

const submission = {
  id: 17,
  job_id: 31,
  cost_center_id: 1,
  submitted_by: 23,
  submitter_username: 'student@ctechigh.org',
  item_name: 'robot-bracket.3mf',
  status: 'submitted',
  approved_printer_id: null,
  lifecycle_revision: 2,
  compatibility_engine_version: null,
  rejection_reason: null,
  created_at: '2026-09-19T13:00:00Z',
  updated_at: '2026-09-19T13:00:00Z',
}

const commonResponses = {
  'GET /api/license': {
    valid: true,
    tier: 'education',
    licensee: 'CTEC POC',
    features: ['rbac', 'permissions', 'education_workflows', 'usage_reports', 'analytics', 'maintenance', 'products'],
    max_printers: 50,
    max_users: 500,
    managed_externally: true,
  },
  'GET /api/setup/status': { needs_setup: false },
  'GET /api/branding': {},
  'GET /api/orgs/1/settings': {},
  'GET /api/settings/language': { language: 'en' },
  'GET /api/pricing-config': { ui_mode: 'advanced' },
  'GET /api/settings/education-mode': { enabled: true },
  'GET /api/alerts/unread-count': { count: 0 },
  'GET /api/printers': [{ id: 7, name: 'Bambu P1S Pilot', nickname: 'Bambu P1S Pilot', type: 'bambu', status: 'idle', last_seen: '2026-09-19T13:00:00Z', filament_slots: [] }],
  'GET /api/stats': {},
  'GET /api/auth/capabilities': { smtp_enabled: false },
  'GET /api/auth/oidc/config': { enabled: true },
  'POST /api/auth/ws-token': { token: '' },
  'GET /api/orgs': [],
  'GET /api/education/cost-centers': { items: [center], next_cursor: null },
  'GET /api/education/readiness': {
    education_license: true,
    education_mode: true,
    oidc: { ready: true, provider: 'google', enabled: true },
    classroom: {
      configured: true,
      state: 'connected',
      connected: true,
      client_id: 'ctec-poc.apps.googleusercontent.com',
      account_email: 'teacher@ctechigh.org',
      allowed_domains: 'ctechigh.org',
      last_success_at: '2026-09-19T13:00:00Z',
      last_error_code: null,
    },
    pilot: { active_centers: 1, student_grants: 3, manager_grants: 1, printer_entitlements: 1 },
    backup: { database_backend: 'sqlite', verified_workflow_available: true },
  },
  'GET /api/education/classroom/status': {
    configured: true,
    state: 'connected',
    connected: true,
    client_id: 'ctec-poc.apps.googleusercontent.com',
    account_email: 'teacher@ctechigh.org',
    allowed_domains: 'ctechigh.org',
    last_success_at: '2026-09-19T13:00:00Z',
    last_error_code: null,
  },
  'GET /api/education/classroom/courses': {
    items: [{ id: 'course-123', name: 'Engineering Design', section: 'Period 2', description: 'CTEC pilot', course_state: 'ACTIVE' }],
  },
  'GET /api/education/classroom/courses/course-123/preview': {
    course: { id: 'course-123', name: 'Engineering Design', section: 'Period 2', description: 'CTEC pilot', course_state: 'ACTIVE' },
    teachers: [{ provider_user_id: 'teacher-1', name: 'Aaron Teacher', email: 'teacher@ctechigh.org' }],
    students: [
      { provider_user_id: 'student-1', name: 'Avery Student', email: 'avery@ctechigh.org' },
      { provider_user_id: 'student-2', name: 'Jordan Student', email: 'jordan@ctechigh.org' },
    ],
    mapping: null,
    diff: { added_or_changed: ['teacher-1', 'student-1', 'student-2'], removed: [], unchanged: 0 },
  },
  'GET /api/education/submissions/17/compatibility': {
    submission_id: 17,
    printer_id: 7,
    lifecycle_revision: 2,
    compatible: true,
    reasons: [],
    engine_version: 'poc-v1',
  },
  'GET /api/education/cost-centers/1/printers': {
    items: [{ entitlement_id: 8, printer_id: 7, name: 'Bambu P1S Pilot', machine_type: 'P1S', api_type: 'bambu', state: 'active', granted_at: '2026-09-19T12:00:00Z', revoked_at: null }],
    next_cursor: null,
    center_revision: 4,
  },
}

function personaResponse(persona, method, pathname) {
  const role = persona === 'admin' ? 'admin' : persona === 'manager' ? 'operator' : 'viewer'
  if (method === 'GET' && pathname === '/api/auth/me') {
    return { id: persona === 'student' ? 23 : persona === 'manager' ? 12 : 1, username: `${persona}@ctechigh.org`, email: `${persona}@ctechigh.org`, role, group_id: 1, group_name: 'CTEC High' }
  }
  if (method === 'GET' && pathname === '/api/permissions') {
    return { page_access: { education: [role], settings: ['admin'], audit: ['admin'] }, action_access: {} }
  }
  if (method === 'GET' && pathname === '/api/education/capabilities') {
    return {
      education_enabled: true,
      student: persona === 'student',
      manager: persona === 'manager',
      tenant_admin: persona === 'admin',
      student_cost_center_ids: persona === 'student' ? [1] : [],
      managed_cost_center_ids: persona === 'manager' ? [1] : [],
    }
  }
  if (method === 'GET' && pathname === '/api/education/submissions') {
    return { items: persona === 'student' ? [] : [submission], next_cursor: null }
  }
  if (method === 'POST' && pathname === '/api/education/submissions') {
    return { id: 18, job_id: 32, print_file_id: 41, model_id: 51, cost_center_id: 1, status: 'submitted', lifecycle_revision: 1 }
  }
  if (method === 'POST' && pathname === '/api/education/submissions/17/approve') {
    return { ...submission, status: 'pending', approved_printer_id: 7, lifecycle_revision: 3, compatibility_engine_version: 'poc-v1' }
  }
  if (method === 'POST' && pathname === '/api/education/classroom/courses/course-123/import') {
    return { course_id: 'course-123', cost_center_id: 1, created_cost_center: true, teachers: 1, students: 2, revision: 1 }
  }
  return undefined
}

function responseFor(persona, method, pathname) {
  const dynamic = personaResponse(persona, method, pathname)
  if (dynamic !== undefined) return dynamic
  return commonResponses[`${method} ${pathname}`]
}

async function configurePage(page, persona, theme, finding) {
  await page.addInitScript(({ selectedTheme }) => {
    localStorage.setItem('odin-theme', selectedTheme === 'light' ? 'light' : 'dark')
    localStorage.setItem('odin-locale', 'en')
  }, { selectedTheme: theme })
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url())
    const method = route.request().method()
    finding.requests.push(`${method} ${url.pathname}${url.search}`)
    const body = responseFor(persona, method, url.pathname)
    if (body === undefined) {
      finding.unexpected.push(`${method} ${url.pathname}${url.search}`)
      await route.fulfill({ status: 599, contentType: 'application/json', body: JSON.stringify({ error: 'undeclared CTEC UI fixture endpoint' }) })
      return
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  })
}

async function inspectPage(page, finding) {
  const axe = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa'])
    .analyze()
  finding.axeFindings = axe.violations.map(item => ({
    id: item.id,
    impact: item.impact,
    help: item.help,
    targets: item.nodes.map(node => node.target),
  }))
  finding.serious = finding.axeFindings.filter(item => ['serious', 'critical'].includes(item.impact))
  const layout = await page.evaluate(() => ({
    documentWidth: document.documentElement.scrollWidth,
    viewportWidth: document.documentElement.clientWidth,
    localStorageKeys: Object.keys(localStorage),
    sessionStorageKeys: Object.keys(sessionStorage),
  }))
  finding.assertions.push({ name: 'no horizontal document overflow', pass: layout.documentWidth <= layout.viewportWidth + 1, detail: layout })
  const sensitiveKeys = [...layout.localStorageKeys, ...layout.sessionStorageKeys].filter(key => /user|token|roster|submission|grant|classroom|oidc/i.test(key))
  finding.assertions.push({ name: 'no sensitive browser persistence', pass: sensitiveKeys.length === 0, detail: sensitiveKeys })
}

const cases = [
  {
    id: 'student-mobile-upload', persona: 'student', theme: 'light', viewport: { width: 390, height: 844 },
    scenario: async (page, assertions) => {
      await page.getByRole('heading', { name: 'Education', exact: true }).waitFor()
      const trigger = page.getByRole('button', { name: 'Submit print', exact: true }).first()
      await trigger.click()
      const dialog = page.getByRole('dialog', { name: 'Submit a print' })
      await dialog.waitFor()
      assertions.push({ name: 'student sees authorized center', pass: await dialog.getByText(/Engineering Design/).count() > 0 })
      assertions.push({ name: 'sliced Bambu boundary is visible', pass: await dialog.getByText(/sliced Bambu 3MF/i).count() > 0 })
      await dialog.locator('input[type=file]').setInputFiles({ name: 'robot-bracket.3mf', mimeType: 'application/vnd.ms-package.3dmanufacturing-3dmodel+xml', buffer: Buffer.from('PK\u0003\u0004') })
      assertions.push({ name: 'valid .3mf enables submission', pass: await dialog.getByRole('button', { name: 'Submit for review' }).isEnabled() })
    },
  },
  {
    id: 'manager-tablet-review', persona: 'manager', theme: 'high-contrast', viewport: { width: 768, height: 1024 },
    scenario: async (page, assertions) => {
      await page.getByRole('heading', { name: 'Education', exact: true }).waitFor()
      await page.getByRole('button', { name: 'Review queue', exact: true }).click()
      await page.getByRole('button', { name: 'Review', exact: true }).click()
      const dialog = page.getByRole('dialog', { name: 'Review submission' })
      await dialog.waitFor()
      await dialog.getByLabel('Authorized printer').selectOption('7')
      await dialog.getByText('Compatible — ready for approval').waitFor()
      assertions.push({ name: 'manager sees submitter and authorized printer', pass: await dialog.getByText(/student@ctechigh.org/).count() > 0 && await dialog.getByText(/Bambu P1S Pilot/).count() > 0 })
      assertions.push({ name: 'approval is compatibility gated', pass: await dialog.getByRole('button', { name: 'Approve and queue' }).isEnabled() })
    },
  },
  {
    id: 'admin-desktop-readiness', persona: 'admin', theme: 'dark', viewport: { width: 1440, height: 900 },
    scenario: async (page, assertions) => {
      await page.getByRole('heading', { name: 'Education', exact: true }).waitFor()
      await page.getByRole('heading', { name: 'POC readiness' }).waitFor()
      assertions.push({ name: 'admin sees factual readiness signals', pass: await page.getByText('Pilot roster', { exact: true }).count() > 0 && await page.getByText('Backup workflow', { exact: true }).count() > 0 })
      assertions.push({ name: 'hardware claim is bounded', pass: await page.getByText(/does not certify untested physical printer models/i).count() > 0 })
    },
  },
  {
    id: 'admin-mobile-classroom', persona: 'admin', theme: 'light', viewport: { width: 390, height: 844 },
    scenario: async (page, assertions) => {
      await page.getByRole('heading', { name: 'Education', exact: true }).waitFor()
      await page.getByRole('button', { name: 'Google Classroom', exact: true }).click()
      await page.getByRole('heading', { name: 'Google Classroom roster import' }).waitFor()
      await page.getByRole('button', { name: /Engineering Design/ }).click()
      await page.getByText('Avery Student', { exact: true }).waitFor()
      assertions.push({ name: 'classroom is visibly read-only', pass: await page.getByText(/never requests coursework, grades, or roster write access/i).count() > 0 })
      assertions.push({ name: 'full preview precedes import', pass: await page.getByText('Aaron Teacher', { exact: true }).count() > 0 && await page.getByText('Jordan Student', { exact: true }).count() > 0 })
      assertions.push({ name: 'explicit import action exists', pass: await page.getByRole('button', { name: 'Import course and roster' }).count() > 0 })
    },
  },
]

fs.mkdirSync(outputDir, { recursive: true })
const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] })
const results = []

for (const testCase of cases) {
  const context = await browser.newContext({ viewport: testCase.viewport, reducedMotion: 'reduce' })
  const page = await context.newPage()
  const finding = {
    id: testCase.id,
    persona: testCase.persona,
    theme: testCase.theme,
    viewport: `${testCase.viewport.width}x${testCase.viewport.height}`,
    assertions: [],
    axeFindings: [],
    serious: [],
    consoleErrors: [],
    unexpected: [],
    requests: [],
  }
  page.on('console', message => { if (message.type() === 'error') finding.consoleErrors.push(message.text()) })
  page.on('pageerror', error => finding.consoleErrors.push(String(error)))
  await configurePage(page, testCase.persona, testCase.theme, finding)
  try {
    await page.goto(`${baseURL}/education`, { waitUntil: 'domcontentloaded' })
    if (testCase.theme === 'high-contrast') await page.evaluate(() => document.documentElement.classList.add('high-contrast'))
    await testCase.scenario(page, finding.assertions)
    await inspectPage(page, finding)
  } catch (error) {
    finding.consoleErrors.push(String(error))
  }
  finding.pass = finding.serious.length === 0
    && finding.consoleErrors.length === 0
    && finding.unexpected.length === 0
    && finding.assertions.length > 0
    && finding.assertions.every(assertion => assertion.pass)
  const screenshot = path.join(outputDir, `${testCase.id}.png`)
  await page.screenshot({ path: screenshot, fullPage: true })
  finding.screenshot = path.basename(screenshot)
  results.push(finding)
  await context.close()
}

await browser.close()

const passed = results.filter(result => result.pass).length
const report = { generated_at: now, base_url: baseURL, summary: { passed, total: results.length }, results }
fs.writeFileSync(path.join(outputDir, 'education-workbench-audit.json'), `${JSON.stringify(report, null, 2)}\n`)

const escape = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
const rows = results.map(result => `
  <article class="case ${result.pass ? 'pass' : 'fail'}">
    <div class="row"><h2>${escape(result.id)}</h2><span>${result.pass ? 'PASS' : 'FAIL'}</span></div>
    <p>${escape(result.persona)} · ${escape(result.theme)} · ${escape(result.viewport)}</p>
    <img src="${escape(result.screenshot)}" alt="Rendered ${escape(result.id)} Education workbench state">
    <ul>${result.assertions.map(assertion => `<li>${assertion.pass ? '✓' : '✕'} ${escape(assertion.name)}</li>`).join('')}</ul>
    <p>Serious/critical axe findings: ${result.serious.length}; console errors: ${result.consoleErrors.length}; undeclared API requests: ${result.unexpected.length}</p>
    ${result.consoleErrors.length ? `<pre>${escape(result.consoleErrors.join('\n'))}</pre>` : ''}
    ${result.unexpected.length ? `<pre>${escape(result.unexpected.join('\n'))}</pre>` : ''}
  </article>`).join('')
fs.writeFileSync(path.join(outputDir, 'index.html'), `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>CTEC POC Education UI Evidence</title><style>
:root{color-scheme:dark;font-family:IBM Plex Sans,system-ui,sans-serif;background:#0a0c10;color:#e8ecf2}body{max-width:1120px;margin:auto;padding:32px}header,.case{border:1px solid #252d3d;background:#0f1218;border-radius:10px;padding:20px;margin-bottom:18px}.row{display:flex;align-items:center;justify-content:space-between;gap:16px}h1,h2{margin:0}p,li{color:#aab3c3}.case span{font:700 12px ui-monospace,monospace}.pass span{color:#5ad69b}.fail span{color:#ff7777}img{display:block;width:100%;max-height:640px;object-fit:contain;object-position:top;background:#080a0e;border:1px solid #252d3d;border-radius:7px;margin:16px 0}pre{white-space:pre-wrap;color:#ffaaaa}a{color:#e69b3a}
</style></head><body><header><h1>CTEC POC Education UI Evidence</h1><p>Compiled frontend · ${passed}/${results.length} cases passed · generated ${escape(now)}</p></header>${rows}</body></html>`)

if (passed !== results.length) {
  console.error(`CTEC Education UI audit failed: ${passed}/${results.length} cases passed`)
  process.exit(1)
}
console.log(`CTEC Education UI audit passed: ${passed}/${results.length}; ${outputDir}/index.html`)
