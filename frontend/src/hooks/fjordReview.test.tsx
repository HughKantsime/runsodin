import { cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, expect, it, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { readFileSync, readdirSync } from 'node:fs'
import ts from 'typescript'
import { brandingColors } from '../BrandingContext'
import postcss from 'postcss'
import tailwind from '@tailwindcss/postcss'
import { transform, Features } from 'lightningcss'
import * as csstree from 'css-tree'
import { PrinterCard as DashboardCard } from '../pages/dashboard/Dashboard'
import TVDashboard from '../pages/dashboard/TVDashboard'
import PrinterCard from '../components/printers/PrinterCard'
import { FilamentSlotsPanel } from '../components/printers/PrinterPanels'
import JobRow, { JobCardList } from '../components/jobs/JobRow'
import SubmissionSliceInfo from '../components/education/SubmissionSliceInfo'
import ReadinessPanel from '../components/education/ReadinessPanel'
import { SupportLevelBadge } from '../components/printers/SupportLevel'
import AlertBell from '../components/notifications/AlertBell'
import { NavCount } from '../components/layout/Sidebar'
import GeneralTab from '../components/admin/GeneralTab'
import HmsHistoryPanel from '../components/printers/HmsHistoryPanel'
import { contrastRatio } from '../utils/contrast'

vi.mock('../permissions', () => ({ canDo: () => true, getCurrentUser: () => ({ id: 1, role: 'operator' }) }))
vi.mock('../api', async original => ({ ...await original<any>(), alerts: { unreadCount: vi.fn().mockResolvedValue({ unread_count: 3 }) },
  pricingConfig: { get: vi.fn().mockResolvedValue({ ui_mode: 'advanced' }), update: vi.fn().mockResolvedValue({}) },
  config: { get: vi.fn().mockResolvedValue({}), getNetwork: vi.fn().mockResolvedValue({}), getRemoteAccess: vi.fn().mockResolvedValue({}) },
  fetchAPI: vi.fn().mockResolvedValue({}),
  printerTelemetry: { hmsHistory: vi.fn().mockResolvedValue({ total: 2, frequency: {}, entries: [
    { id: 1, severity: 'critical', message: 'Critical fault', occurred_at: '2026-11-16T12:00:00Z' },
    { id: 2, severity: 'warning', message: 'Warning fault', occurred_at: '2026-11-16T12:00:00Z' },
  ] }) } }))

// Compile real utilities and nested CSS in memory. JSDOM lacks cascade layers
// and variable resolution, so flatten layers before unlayered overrides and
// resolve computed property variables explicitly. This is not a browser audit.
let rules: postcss.Rule[]
let style: HTMLStyleElement
const classAliases = new Map<string, string>()
beforeAll(async () => {
  const base = await postcss([tailwind()]).process(readFileSync('src/index.css', 'utf8'), { from: 'src/index.css' })
  const css = transform({ filename: 'colors.css', code: Buffer.from(base.css + readFileSync('src/fjord-tokens.css', 'utf8') + readFileSync('src/fjord.css', 'utf8')), include: Features.Nesting }).code.toString()
  const layered: postcss.Rule[] = [], overrides: postcss.Rule[] = []
  postcss.parse(css).walkRules(rule => {
    // Keyframe percentages are not element selectors (opacity is tested below).
    if (rule.parent?.type === 'atrule' && rule.parent.name.endsWith('keyframes')) return
    let layer = false
    for (let p = rule.parent; p; p = p.parent) if (p.type === 'atrule' && p.name === 'layer') layer = true
    ;(layer ? layered : overrides).push(rule)
  })
  rules = [...layered, ...overrides]
  style = document.createElement('style')
  document.head.append(style)
})
afterEach(() => { cleanup(); document.documentElement.className = ''; delete document.documentElement.dataset.themeFamily; document.documentElement.removeAttribute('style') })

function theme(mode: 'dark' | 'light', family = 'fjord') {
  document.documentElement.className = mode === 'light' ? 'light' : ''
  document.documentElement.dataset.themeFamily = family
  const css = rules.map(rule => {
    if (rule.selector.includes('::')) return '' // Only element colors are under test.
    const declarations = rule.nodes.filter(n => n.type === 'decl' && ['color', 'background-color', 'opacity', 'fill'].includes(n.prop))
      .map(n => { const d = n as postcss.Declaration; return `${d.prop}:${resolve(d.value)}${d.important ? ' !important' : ''}` })
    return declarations.length ? `${rule.selector.replaceAll(':hover', '.test-hover')} {${declarations.join(';')}}` : ''
  }).join('\n')
  const resolved = postcss.parse(transform({ code: Buffer.from(css), targets: { chrome: 100 << 16 } }).code.toString())
  const renderedRules: string[] = []
  resolved.walkRules(rule => {
    // Lightning CSS emits sRGB fallbacks before wide-gamut declarations, which
    // JSDOM cannot compute. Use the fallback, including its alpha channel.
    const declarations = rule.nodes.filter(n => n.type === 'decl' && !/\b(?:oklab|oklch|lab|lch|color)\(/.test(n.value))
    const selector = csstree.parse(rule.selector, { context: 'selectorList' })
    csstree.walk(selector, node => {
      if (node.type === 'ClassSelector') {
        const name = csstree.ident.decode(node.name)
        if (!classAliases.has(name)) classAliases.set(name, `css-class-${classAliases.size}`)
        node.name = classAliases.get(name)!
      }
    })
    selector.children.forEach(part => {
      renderedRules.push(`${csstree.generate(part).replace(/"i\]/g, '" i]')} {${declarations.join(';')}}`)
    })
  })
  style.textContent = renderedRules.join('\n')
}
let variableState = ''
let rootVariables: Record<string, string>
function variables(): Record<string, string> {
  const root = document.documentElement
  // Every sample shares these variables. Re-scan only when theme/branding
  // changes, including changes made by a mounted component after theme().
  const state = JSON.stringify([root.className, root.dataset.themeFamily, root.getAttribute('style')])
  if (state === variableState) return rootVariables
  const vars: Record<string, string> = {}
  for (const rule of rules) {
    if (rule.nodes.some(n => n.type === 'decl' && n.prop.startsWith('--')) && document.documentElement.matches(rule.selector)) {
      rule.nodes.forEach(n => { if (n.type === 'decl' && n.prop.startsWith('--')) vars[n.prop] = !n.important && document.documentElement.style.getPropertyValue(n.prop) || n.value })
    }
  }
  variableState = state
  return rootVariables = vars
}
function resolve(value: string, vars = variables()): string {
  return value.replace(/var\((--[\w-]+)(?:,\s*([^()]+))?\)/g, (_, key, fallback) => resolve(vars[key] ?? fallback ?? key, vars)).replace(/^#([a-f\d])([a-f\d])([a-f\d])$/i, '#$1$1$2$2$3$3')
}
const colorDocument = document.implementation.createHTMLDocument('')
function color(el: Element, prop = 'color') {
  // JSDOM also mis-parses commas inside escaped class/attribute selectors.
  // Give every compiled class an equivalent simple selector on the real DOM.
  for (const node of document.querySelectorAll('*')) for (const name of [...node.classList]) {
    const alias = classAliases.get(name)
    if (alias) node.classList.add(alias)
  }
  // Select the winning declaration explicitly: JSDOM's computed-style cache
  // can retain a previous declaration even when matches() finds the override.
  let selected = '', rank = -1
  for (const r of style.sheet!.cssRules as any) {
    const value = r.style?.getPropertyValue(prop)
    if (!value || !el.matches(r.selectorText)) continue
    const priority = (r.style.getPropertyPriority(prop) === 'important' ? 1e9 : 0) + specificity(csstree.parse(r.selectorText, { context: 'selector' }))
    if (priority >= rank) { selected = value; rank = priority }
  }
  const inline = (el as HTMLElement).style.getPropertyValue(prop)
  const value = resolve(inline && rank < 1e9 ? inline : selected || (prop === 'fill' ? el.getAttribute('fill') || '' : ''))
  if (!value && prop === 'background-color') return 'rgba(0, 0, 0, 0)'
  if (prop === 'opacity') return value || '1'
  if (!value && prop === 'color' && el.parentElement) return color(el.parentElement)
  const css = transform({ filename: 'color.css', code: Buffer.from(`a{${prop}:${value}}`), minify: true, targets: { chrome: 100 << 16 } }).code.toString()
  const probe = colorDocument.createElement('span')
  probe.style.color = css.slice(css.indexOf(':') + 1, -1)
  // The cascade and variables are already resolved. An empty document still
  // normalizes named/alpha colors without re-evaluating the full stylesheet.
  const result = getComputedStyle(probe).color
  const rgb = result.match(/^rgb\((\d+), (\d+), (\d+)\)$/)
  return rgb ? '#' + rgb.slice(1).map(n => Number(n).toString(16).padStart(2, '0')).join('') : result
}
function specificity(node: any): number {
  if (node.type === 'IdSelector') return 10000
  if (node.type === 'ClassSelector' || node.type === 'AttributeSelector') return 100
  if (node.type === 'TypeSelector') return node.name === '*' ? 0 : 1
  if (node.type === 'PseudoClassSelector') {
    if (node.name === 'where') return 0
    if (['is', 'not', 'has'].includes(node.name)) return Math.max(...node.children.toArray().flatMap((list: any) => list.children.toArray().map(specificity)))
    return 100
  }
  return node.children?.toArray().reduce((sum: number, child: any) => sum + specificity(child), 0) ?? 0
}
function token(el: Element, name: string, prop = 'color') {
  const expected = resolve(`var(${name})`).toLowerCase().replace(/^#([a-f\d])([a-f\d])([a-f\d])$/, '#$1$1$2$2$3$3')
  expect(color(el, prop)).toBe(expected)
}
it('reuses root variables for repeated resolutions and refreshes them when branding changes', () => {
  theme('light')
  const expected = resolve('var(--brand-primary)')
  const matches = vi.spyOn(document.documentElement, 'matches')
  try {
    for (let i = 0; i < 10; i++) expect(resolve('var(--brand-primary)')).toBe(expected)
    expect(matches.mock.calls.length).toBe(0)
    document.documentElement.style.setProperty('--brand-primary', '#123456')
    expect(resolve('var(--brand-primary)')).toBe('#123456')
    document.documentElement.removeAttribute('style')
    expect(resolve('var(--brand-primary)')).toBe(expected)
  } finally { matches.mockRestore() }
})
it('normalizes resolved colors without attaching a probe to the full stylesheet', () => {
  theme('dark')
  const el = document.createElement('button')
  el.style.color = '#abc'
  el.style.backgroundColor = 'rgba(6, 78, 59, 0.5)'
  document.body.append(el)
  const append = vi.spyOn(document.body, 'append')
  try {
    expect(color(el)).toBe('#aabbcc')
    expect(color(el, 'background-color')).toBe('rgba(6, 78, 59, 0.5)')
    el.style.color = 'red'
    expect(color(el)).toBe('#ff0000')
    el.style.backgroundColor = 'transparent'
    expect(color(el, 'background-color')).toBe('rgba(0, 0, 0, 0)')
    expect(append.mock.calls.length).toBe(0)
  } finally { append.mockRestore(); el.remove() }
})
const base = { id: 1, name: 'Test printer', is_active: true, api_type: 'bambu', gcode_state: 'IDLE', last_seen: new Date().toISOString(), filament_slots: [], tags: [], maintenance: { state: 'ok' } }
const cardProps = { allFilaments: [], spools: [], onDelete: vi.fn(), onToggleActive: vi.fn(), onUpdateSlot: vi.fn(), onEdit: vi.fn() }
function mount(ui: React.ReactNode, seed: Array<[unknown[], unknown]> = []) {
  const client = new QueryClient({ defaultOptions: { queries: { enabled: false, retry: false } } })
  for (const [key, data] of seed) client.setQueryData(key, data)
  return render(<QueryClientProvider client={client}><MemoryRouter>{ui}</MemoryRouter></QueryClientProvider>)
}
function row(dispatching = false, mobile = false) {
  const job = { id: 2, item_name: 'Test job', status: 'scheduled', printer_id: 1, printer: { name: 'Test printer' }, quantity: 1, hold: true, is_locked: false, priority: 3, waiting_reason: { code: 'printer_busy', printer_id: 1 } }
  return mount(mobile ? <JobCardList jobs={[job]} isLoading={false} emptyText="No jobs" selectedIds={new Set()} onToggleSelect={vi.fn()} onSelectAll={vi.fn()} allSelected={false} onAction={vi.fn()} printers={[]} /> : <table><tbody><JobRow job={job} onAction={vi.fn()} isSelected={false} onToggleSelect={vi.fn()} isDispatching={dispatching} /></tbody></table>)
}

for (const mode of ['dark', 'light'] as const) {
  it.each(['pause', 'assignment', 'ledger unavailable', 'ledger partial', 'low spool', 'spool changed'])('keeps %s warning amber in Fjord ' + mode, warning => {
    theme(mode)
    const slot = { id: 1, slot_number: 1, color_hex: 'ffffff', remaining_weight: 50, spool_change_pending: true }
    if (warning === 'low spool' || warning === 'spool changed') {
      mount(<FilamentSlotsPanel printer={{ ...base, filament_slots: [slot] } as any} />)
      token(screen.getByText(warning === 'low spool' ? '50g' : /Spool may have changed/), '--status-warning')
    } else {
      mount(<PrinterCard printer={{ ...base, gcode_state: 'PAUSE', filament_slots: [slot], ...(warning.startsWith('ledger') ? { filament_source: 'filament-ledger', filament_source_status: warning.split(' ')[1] } : {}) }} {...cardProps} />)
      const el = warning === 'pause' ? screen.getByText('Paused: Cause unavailable') : warning === 'assignment' ? screen.getByTitle('Slots need spool assignment') : screen.getByText(warning.endsWith('partial') ? /Some slots could not/ : /Filament Ledger is unavailable/)
      token(el, '--status-warning')
    }
  })
  it('keeps experimental support warnings and their tint in Fjord ' + mode, () => {
    theme(mode); mount(<SupportLevelBadge printer={{ id: 1, api_type: 'elegoo', support_level: 'experimental' }} />)
    const badge = screen.getByTestId('support-badge')
    token(badge, '--status-warning'); token(badge, '--fjord-status-warning-bg', 'background-color')
  })
  it('keeps filament mismatch warnings in Fjord ' + mode, () => {
    theme(mode)
    mount(<SubmissionSliceInfo id={1} revision={1} printerId={1} />, [[['education-slice-info', '', 1, 1, 1, undefined], {
      plates: [{ plate: '1', estimated_seconds: '', filaments: [{ source_slot: '1', material: 'PETG', color: '' }] }],
      selected_plate_index: 0, slots: [{ slot_number: 1, filament_type: 'PLA' }], freshness: 'Current',
    }]])
    token(screen.getByRole('alert'), '--status-warning')
  })
  it('keeps readiness problems in Fjord ' + mode, () => {
    theme(mode)
    mount(<ReadinessPanel />, [[['education-readiness', undefined], { education_license: true, education_mode: true, oidc: { ready: false }, classroom: { connected: false }, pilot: { complete_centers: 0 }, backup: { verified_workflow_available: false } }]])
    for (const icon of screen.getAllByLabelText('Needs attention')) token(icon, '--status-warning')
  })
  for (const mobile of [false, true]) it.each(['P3', 'Held', 'Open printer'])('keeps %s informational in Fjord ' + mode + (mobile ? ' phone' : ' table'), label => {
    theme(mode); row(false, mobile)
    const el = screen.getByText(label)
    token(el, label === 'Open printer' ? '--status-printing' : '--fjord-status-neutral')
    if (label !== 'Open printer') token(el, '--fjord-status-neutral-bg', 'background-color')
  })
  it.each(['header', 'footer'])('uses ready for dashboard %s dot and label in Fjord ' + mode, location => {
    theme(mode)
    mount(<DashboardCard printer={base} hasCamera={false} onCameraClick={vi.fn()} onClick={vi.fn()} />)
    const label = screen.getAllByText('Idle')[location === 'header' ? 0 : 1]
    token(label, '--fjord-status-ready'); token(label.previousElementSibling!, '--fjord-status-ready', 'background-color')
  })
  it('uses ready for idle Printers card in Fjord ' + mode, () => {
    theme(mode); mount(<PrinterCard printer={base} {...cardProps} />)
    const label = screen.getByTestId('printer-status')
    token(label, '--fjord-status-ready'); token(label.previousElementSibling!, '--fjord-status-ready', 'background-color')
  })
  it('retains non-idle connectivity colors in Fjord ' + mode, () => {
    theme(mode); mount(<PrinterCard printer={{ ...base, gcode_state: 'RUNNING' }} {...cardProps} />)
    const label = screen.getByTestId('printer-status')
    token(label, '--status-completed'); token(label.previousElementSibling!, '--status-completed', 'background-color')
  })
  it('retains ready for TV idle dot and label in Fjord ' + mode, () => {
    theme(mode); mount(<TVDashboard />, [[['printers'], [base]], [['alert-summary-tv'], { unread_count: 0 }]])
    const label = screen.getByTestId('tv-printer-status')
    token(label, '--fjord-status-ready'); token(label.previousElementSibling!, '--fjord-status-ready', 'background-color')
  })
  it('matches bell and sidebar count colors with 4.5:1 contrast in Fjord ' + mode, async () => {
    theme(mode); mount(<><AlertBell /><NavCount count={3} label="Unread alerts" tone="hot" /></>)
    const bell = await screen.findByRole('button', { name: 'Notifications, 3 unread' })
    const badge = bell.querySelector('span')!
    const sidebar = screen.getByRole('status')
    token(badge, '--fjord-on-failed'); token(badge, '--status-failed', 'background-color')
    expect(color(badge)).toBe(color(sidebar)); expect(color(badge, 'background-color')).toBe(color(sidebar, 'background-color'))
    expect(contrastRatio(color(badge), color(badge, 'background-color'))).toBeGreaterThanOrEqual(4.5)
  })
  for (const family of ['classic', 'fjord']) {
    it('resolves Dispatch text and hover in ' + family + ' ' + mode, () => {
      theme(mode, family); row()
      const button = screen.getByRole('button', { name: 'Dispatch to printer' })
      const expected = family === 'classic' ? mode === 'dark' ? '#34d399' : '#136c34' : mode === 'dark' ? '#3ddc74' : '#0d3c1b'
      expect(color(button)).toBe(expected)
      button.classList.add('test-hover')
      if (mode === 'dark') token(button, '--brand-text-primary')
      else expect(color(button)).toBe(expected)
      if (family === 'classic' && mode === 'dark') expect(color(button, 'background-color')).toBe('rgba(6, 78, 59, 0.5)')
      else token(button, '--brand-surface', 'background-color')
    })
  }
}
it('restores Classic dark Dispatch loading emerald', () => {
  theme('dark', 'classic'); row(true)
  expect(color(screen.getByRole('button', { name: 'Dispatching...' }))).toBe('#10b981')
})

// Composite actual compiled colors, inherited text, and opacity over the card.
function composite(fg: string, bg: string, opacity = 1): string {
  bg = bg.replace(/^#([a-f\d])([a-f\d])([a-f\d])$/i, '#$1$1$2$2$3$3')
  const rgba = fg.match(/^rgba?\(([^)]+)\)$/)?.[1].split(',').map(Number)
  const channels = rgba?.slice(0, 3) ?? [1, 3, 5].map(i => parseInt(fg.slice(i, i + 2), 16))
  const alpha = opacity * (rgba?.[3] ?? 1)
  return '#' + channels.map((n, i) => Math.round(n * alpha + parseInt(bg.slice(1 + i * 2, 3 + i * 2), 16) * (1 - alpha)).toString(16).padStart(2, '0')).join('')
}
for (const mode of ['light', 'dark'] as const) {
  it.each(['simple', 'advanced'])('Interface Mode labels and descriptions meet 4.5:1 with %s selected in Fjord ' + mode, async selected => {
    localStorage.setItem('odin-theme', mode); localStorage.setItem('odin-theme-family', 'fjord')
    theme(mode); mount(<GeneralTab />)
    await screen.findByText('Vigil AI settings tab will be hidden')
    fireEvent.click(screen.getByText(selected === 'simple' ? 'Simple' : 'Advanced'))
    for (const name of ['Simple', 'Advanced']) {
      const button = screen.getByText(name).closest('button')!
      const fill = composite(color(button, 'background-color'), resolve('var(--brand-card-bg)'))
      for (const label of [...button.children].slice(0, 2)) {
        const text = composite(color(label), fill, Number(color(label, 'opacity')))
        expect.soft(contrastRatio(text, fill), `${mode} ${selected} selected: ${label.textContent}: ${text} on ${fill}`).toBeGreaterThanOrEqual(4.5)
      }
    }
    for (const label of ['Theme family', 'Appearance', 'Density']) {
      const select = screen.getByLabelText(label)
      expect(contrastRatio(color(select), color(select, 'background-color')), label).toBeGreaterThanOrEqual(4.5)
    }
  })
  it('maps the Vigil caution to full warning text in Fjord ' + mode, async () => {
    localStorage.setItem('odin-theme', mode); localStorage.setItem('odin-theme-family', 'fjord')
    theme(mode); mount(<GeneralTab />)
    token(await screen.findByText('Vigil AI settings tab will be hidden'), '--status-warning')
  })
  it('maps critical and warning HMS severity text and fills in Fjord ' + mode, async () => {
    theme(mode); mount(<HmsHistoryPanel printerId={1} />)
    await waitFor(() => expect(screen.getByText('critical')).toBeVisible())
    for (const [label, status] of [['critical', 'failed'], ['warning', 'warning']]) {
      const badge = screen.getByText(label)
      token(badge, `--status-${status}`)
      token(badge, `--fjord-status-${status}-bg`, 'background-color')
      expect(contrastRatio(color(badge), color(badge, 'background-color'))).toBeGreaterThanOrEqual(4.5)
    }
  })
}

// Scan production source for solid brand fills, preserving each literal branch
// of conditional classes. Testing the resulting cascade permits Classic's
// legacy colors while rejecting an uncorrected hard-coded color in Fjord.
function brandSamples(dir = 'src'): Array<{ location: string; classes?: string; background?: string; foreground?: string }> {
  return readdirSync(dir, { withFileTypes: true }).flatMap(entry => {
    const file = `${dir}/${entry.name}`
    if (entry.isDirectory()) return entry.name === 'test' ? [] : brandSamples(file)
    if (!/\.[jt]sx?$/.test(file) || /\.(test|spec)\./.test(file)) return []
    const source = ts.createSourceFile(file, readFileSync(file, 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
    const samples: ReturnType<typeof brandSamples> = []
    function visit(node: ts.Node) {
      const location = `${file}:${source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1}`
      if (ts.isStringLiteralLike(node) || ts.isTemplateHead(node) || ts.isTemplateMiddle(node) || ts.isTemplateTail(node)) {
        if (/(?:^|\s)bg-\[var\(--brand-(?:primary|accent)\)\](?=\s|$)/.test(node.text)) samples.push({ location, classes: node.text.trim() })
      }
      if (ts.isObjectLiteralExpression(node)) {
        const props = node.properties.filter(ts.isPropertyAssignment)
        const bg = props.find(p => p.name.getText(source) === 'backgroundColor')?.initializer
        if (bg && ts.isStringLiteral(bg) && /^var\(--brand-(primary|accent)\)$/.test(bg.text)) {
          const fg = props.find(p => p.name.getText(source) === 'color')?.initializer
          const value = fg && ts.isConditionalExpression(fg) ? fg.whenFalse : fg
          samples.push({ location, background: bg.text, foreground: value && ts.isStringLiteral(value) ? value.text : undefined })
        }
      }
      ts.forEachChild(node, visit)
    }
    visit(source)
    return samples
  })
}
const solidBrandSamples = brandSamples()
for (const mode of ['light', 'dark'] as const) {
  for (const custom of [false, true]) it(`all source-discovered primary/accent fills use the accessible pair in Fjord ${mode}, custom=${custom}`, () => {
    if (custom) {
      const colors = brandingColors({ primary_color: mode === 'light' ? '#345678' : '#6099cf', accent_color: '#123456' }, mode === 'light', true)
      for (const [key, value] of Object.entries(colors)) if (value) document.documentElement.style.setProperty(key, value)
    }
    theme(mode)
    expect(solidBrandSamples.length).toBeGreaterThan(50)
    const failures: string[] = []
    for (const sample of solidBrandSamples) {
      const el = document.createElement('button')
      el.className = sample.classes || ''
      if (sample.background) el.style.backgroundColor = sample.background
      if (sample.foreground) el.style.color = sample.foreground
      el.innerHTML = '<svg><path stroke="currentColor" /></svg>'
      document.body.append(el)
      for (const hover of [false, true]) {
        el.classList.toggle('test-hover', hover)
        const background = composite(color(el, 'background-color'), resolve('var(--brand-card-bg)'))
        const foreground = color(el)
        const accent = sample.background?.includes('--brand-accent') || sample.classes?.includes('bg-[var(--brand-accent)]') && (hover || sample.classes.split(/\s+/).includes('bg-[var(--brand-accent)]'))
        const expected = resolve(`var(${accent ? '--fjord-on-accent' : '--brand-on-primary'})`).replace(/^#([a-f\d])([a-f\d])([a-f\d])$/, '#$1$1$2$2$3$3')
        if (foreground !== expected || !(contrastRatio(foreground, background) >= 4.5)) failures.push(`${sample.location}: ${foreground} on ${background}, hover=${hover}`)
      }
      el.remove()
    }
    expect(failures).toEqual([])
  })
  it('warning labels/tints, archive tags and the Identity tab meet 4.5:1 in Fjord ' + mode, () => {
    theme(mode)
    const { container } = render(<>
      <span className="text-orange-400">Detachment</span>
      <button className="bg-orange-600/20 hover:bg-orange-600/30 text-orange-400">Run Cleanup Now</button>
      <span className="bg-[var(--brand-primary)]/20 text-[var(--brand-primary)] rounded text-xs">review</span>
      <button style={{ backgroundColor: 'var(--brand-sidebar-active-bg)', color: 'var(--brand-accent)' }}>Identity</button>
    </>)
    for (const el of container.children) {
      const fill = composite(color(el, 'background-color'), resolve('var(--brand-surface)'))
      expect.soft(contrastRatio(color(el), fill), el.textContent!).toBeGreaterThanOrEqual(4.5)
    }
    token(screen.getByText('Detachment'), '--status-warning')
    token(screen.getByText('Identity'), '--brand-text-primary')
  })
  it('log empty-state text meets 4.5:1 on black in Fjord ' + mode, () => {
    theme(mode)
    // Read the real empty-state classes so a component change cannot evade this regression.
    const source = readFileSync('src/components/admin/LogViewer.tsx', 'utf8')
    const classes = source.match(/className="([^"]+)">No log lines/)![1]
    render(<div className="bg-black font-mono"><span className={classes}>No log lines</span></div>)
    const label = screen.getByText('No log lines')
    expect(contrastRatio(color(label), color(label.parentElement!, 'background-color'))).toBeGreaterThanOrEqual(4.5)
  })
  it('chart axis ticks and every Analytics legend series meet 4.5:1 in Fjord ' + mode, () => {
    theme(mode)
    const source = readFileSync('src/pages/analytics/Analytics.tsx', 'utf8')
    const tick = source.match(/tick=\{\{ fill: '([^']+)'/)![1]
    const series = [...new Set([...source.matchAll(/<(?:Area|Bar) [^>]*fill="([^"]+)"/g)].map(m => m[1]))]
    expect(series.length).toBeGreaterThan(2)
    const { container } = render(<><svg><text className="recharts-cartesian-axis-tick-value" fill={tick}>10</text></svg>
      {series.map(fill => <span key={fill} className="recharts-legend-item-text" style={{ color: fill }}>Series</span>)}</>)
    const axis = container.querySelector('text')!
    const axisFill = color(axis, 'fill')
    expect.soft(contrastRatio(axisFill, resolve('var(--chart-card-bg)'))).toBeGreaterThanOrEqual(4.5)
    for (const legend of container.querySelectorAll('span')) expect.soft(contrastRatio(color(legend), resolve('var(--chart-card-bg)'))).toBeGreaterThanOrEqual(4.5)
  })
}
