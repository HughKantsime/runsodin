import React, { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import GlobalSearch from '../shared/GlobalSearch'
import Sidebar from './Sidebar'
import MobileHeader from './MobileHeader'

const api = vi.hoisted(() => ({ query: vi.fn(), printerList: vi.fn(), isPro: true, access: vi.fn(() => true) }))
vi.mock('../../api', () => ({
  search: { query: api.query }, printers: { list: api.printerList },
  stats: { get: vi.fn().mockResolvedValue({}) },
  pricingConfig: { get: vi.fn().mockResolvedValue({ ui_mode: 'advanced' }) },
  getEducationMode: vi.fn().mockResolvedValue({ enabled: false }),
  alerts: { unreadCount: vi.fn().mockResolvedValue({ unread_count: 0 }) },
}))
vi.mock('../../BrandingContext', () => ({ useBranding: () => ({ app_name: 'ODIN', app_subtitle: 'Farm' }) }))
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ isPro: api.isPro, isEducation: false }) }))
vi.mock('../../contexts/OrgContext', () => ({ useOrg: () => ({ orgId: 17, orgs: [], isAdmin: false }) }))
vi.mock('../../permissions', () => ({ canAccessPage: api.access, clearSensitiveBrowserState: vi.fn() }))

const clients: QueryClient[] = []
const printers = [
  { id: 1, name: 'Running', gcode_state: 'RUNNING', last_seen: new Date().toISOString() },
  { id: 2, name: 'Idle', gcode_state: 'IDLE', last_seen: new Date().toISOString() },
  { id: 3, name: 'Offline', gcode_state: 'OFFLINE', last_seen: new Date().toISOString() },
]
const results = {
  models: [{ id: 11, name: 'Gear model', qr_code: 'GEAR-M' }],
  jobs: [{ id: 12, name: 'Gear job', status: 'completed' }],
  spools: [{ id: 13, name: 'Gear spool', qr_code: 'GEAR-S' }],
  printers: [{ id: 14, name: 'Gear printer', qr_code: 'GEAR-P' }],
}
function Location() { const l = useLocation(); return <output data-testid="route">{l.pathname}{l.search}</output> }
function show(children: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } }); clients.push(client)
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/']}><Location />{children}</MemoryRouter></QueryClientProvider>)
}
function MobileNavigation() {
  const [open, setOpen] = useState(false)
  return <><MobileHeader onMenuClick={() => setOpen(true)} /><Sidebar mobileOpen={open} onMobileClose={() => setOpen(false)} /></>
}
async function searchGear() {
  const input = screen.getByRole('combobox')
  fireEvent.focus(input); fireEvent.change(input, { target: { value: 'gear' } })
  await screen.findByRole('button', { name: /Gear.*printer.*GEAR.*P/i })
  return input
}
beforeEach(() => {
  vi.clearAllMocks(); api.isPro = true; api.access.mockReturnValue(true); localStorage.clear()
  vi.stubGlobal('__APP_VERSION__', 'audit')
  vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  api.query.mockResolvedValue(results); api.printerList.mockResolvedValue(printers)
})
afterEach(() => { cleanup(); clients.forEach(c => c.clear()); clients.length = 0; vi.unstubAllGlobals() })

describe('F16-16.2-01 sidebar navigation', () => {
  it('exposes the Film timelapse link beside Cameras and respects camera access', () => {
    const view = show(<Sidebar mobileOpen={false} onMobileClose={vi.fn()} />)
    const link = screen.getByRole('link', { name: 'Timelapses' })
    expect(link).toHaveAttribute('href', '/timelapses')
    expect(link.querySelector('.lucide-film')).not.toBeNull()
    fireEvent.click(link)
    expect(screen.getByTestId('route')).toHaveTextContent('/timelapses')
    view.unmount()
    api.access.mockImplementation(page => page !== 'cameras')
    show(<Sidebar mobileOpen={false} onMobileClose={vi.fn()} />)
    expect(screen.queryByRole('link', { name: 'Timelapses' })).not.toBeInTheDocument()
  })
  it('renders authorized advanced page links and navigates by a real link click', async () => {
    show(<Sidebar mobileOpen={false} onMobileClose={vi.fn()} />)
    const nav = screen.getByRole('navigation', { name: 'Main navigation' })
    await within(nav).findByRole('link', { name: 'Orders' })
    for (const [label, path] of [['Dashboard','/'],['Printers','/printers'],['Cameras','/cameras'],['Archives','/archives'],['Projects','/projects'],['Print Log','/print-log'],['Alerts','/alerts'],['Detections','/detections'],['Jobs','/jobs'],['Timeline','/timeline'],['Upload','/upload'],['Models','/models'],['Profiles','/profiles'],['Spools','/spools'],['Calculator','/calculator'],['Settings','/settings'],['Audit Log','/audit'],['Orders','/orders'],['Products','/products'],['Consumables','/consumables'],['Maintenance','/maintenance'],['Analytics','/analytics'],['Utilization','/utilization'],['Usage Reports','/education-reports']]) {
      expect(within(nav).getByRole('link', { name: label })).toHaveAttribute('href', path)
    }
    fireEvent.click(within(nav).getByRole('link', { name: 'Jobs' }))
    expect(screen.getByTestId('route')).toHaveTextContent('/jobs')
  })
  it('collapses to icon links with titles and expands back to readable labels', () => {
    show(<Sidebar mobileOpen={false} onMobileClose={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Collapse sidebar' }))
    expect(screen.getByRole('navigation', { name: 'Main navigation' })).toHaveClass('md:w-16')
    expect(screen.getByTitle('Jobs')).toHaveAttribute('href', '/jobs')
    expect(screen.queryByText('Jobs')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Expand sidebar' }))
    expect(screen.getByRole('link', { name: 'Jobs' })).toBeInTheDocument()
  })
  it('opens the mobile drawer from hamburger and closes it after selecting a page', () => {
    show(<MobileNavigation />)
    const nav = screen.getByRole('navigation', { name: 'Main navigation' })
    expect(nav).toHaveClass('-translate-x-full')
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    expect(nav).toHaveClass('translate-x-0')
    fireEvent.click(within(nav).getByRole('link', { name: 'Spools' }))
    expect(screen.getByTestId('route')).toHaveTextContent('/spools')
    expect(nav).toHaveClass('-translate-x-full')
  })
})
describe('F16-16.2-02 fleet widget', () => {
  it('counts online printers using actual state/time rules and links to printers', async () => {
    show(<Sidebar mobileOpen={false} onMobileClose={vi.fn()} />)
    const fleet = await screen.findByRole('link', { name: 'Fleet status: 2 of 3 printers online' })
    expect(fleet).toHaveTextContent('2/3')
    expect(api.printerList).toHaveBeenCalledWith(false, '', 17)
    fireEvent.click(fleet); expect(screen.getByTestId('route')).toHaveTextContent('/printers')
  })
  it('presents separate printing, idle and offline counts', async () => {
    show(<Sidebar mobileOpen={false} onMobileClose={vi.fn()} />)
    const fleet = await screen.findByRole('link', { name: 'Fleet status: 2 of 3 printers online' })
    expect(fleet).toHaveTextContent(/printing\s*:?\s*1/i)
    expect(fleet).toHaveTextContent(/idle\s*:?\s*1/i)
    expect(fleet).toHaveTextContent(/offline\s*:?\s*1/i)
  })
})
describe('F16-16.2-03 global search and F16-16.3-02 focus', () => {
  it.each(['metaKey','ctrlKey'])('focuses search with %s+K and prevents default', modifier => {
    show(<GlobalSearch />)
    const event = new KeyboardEvent('keydown', { key: 'k', [modifier]: true, bubbles: true, cancelable: true })
    fireEvent(document, event)
    expect(screen.getByRole('combobox')).toHaveFocus(); expect(event.defaultPrevented).toBe(true)
  })
  it('slash focuses search', () => {
    show(<GlobalSearch />); fireEvent.keyDown(document, { key: '/' })
    expect(screen.getByRole('combobox')).toHaveFocus()
  })
  it.each(['input', 'textarea', 'select', 'editable'])('slash preserves typing focus in %s', kind => {
    show(<><GlobalSearch />{kind === 'editable' ? <div contentEditable data-testid="editor" /> : React.createElement(kind, { 'data-testid': 'editor' })}</>)
    const editor = screen.getByTestId('editor'); editor.focus()
    const event = new KeyboardEvent('keydown', { key: '/', bubbles: true, cancelable: true })
    fireEvent(editor, event)
    expect(editor).toHaveFocus(); expect(event.defaultPrevented).toBe(false)
  })
  it('requires two characters and searches/render-highlights the four implemented groups', async () => {
    show(<GlobalSearch />)
    const input = screen.getByRole('combobox'); fireEvent.focus(input)
    fireEvent.change(input, { target: { value: 'g' } })
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument(); expect(api.query).not.toHaveBeenCalled()
    await searchGear()
    expect(api.query).toHaveBeenCalledWith('gear')
    const list = screen.getByRole('listbox', { name: 'Search results' })
    for (const label of ['Models','Jobs','Spools','Printers']) expect(within(list).getByText(label)).toBeInTheDocument()
    expect(list.querySelectorAll('mark').length).toBeGreaterThanOrEqual(4)
    expect(within(list).getByText('completed')).toBeInTheDocument()
  })
  it.each([['model GEAR-M','/models?highlight=11'],['job completed','/jobs?highlight=12'],['spool GEAR-S','/spools?highlight=13'],['printer GEAR-P','/printers?highlight=14']])('selects %s and navigates to its highlight destination', async (suffix, path) => {
    show(<GlobalSearch />); await searchGear()
    fireEvent.click(screen.getByRole('button', { name: new RegExp(`Gear.*${suffix.split(' ').join('.*').replace('GEAR-', 'GEAR.*')}`, 'i') }))
    expect(screen.getByTestId('route').textContent).toBe(path)
    expect(screen.getByRole('combobox')).toHaveValue(''); expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
  })
  it.each(['order', 'product'])('includes %s results in the rendered search dropdown', async entity => {
    api.query.mockResolvedValue({ ...results, orders: [{ id: 15, name: 'Gear order' }], products: [{ id: 16, name: 'Gear product' }] })
    show(<GlobalSearch />); await searchGear()
    const result = screen.getByRole('button', { name: new RegExp(`Gear\\s+${entity}`, 'i') })
    expect(result).toBeInTheDocument()
    fireEvent.click(result)
    expect(screen.getByTestId('route')).toHaveTextContent(entity === 'order' ? '/orders?highlight=15' : '/products?highlight=16')
  })
  it.each(['license', 'permissions'])('hides commercial categories when %s denies access', async boundary => {
    if (boundary === 'license') api.isPro = false
    else api.access.mockReturnValue(false)
    api.query.mockResolvedValue({ ...results, orders: [{ id: 15, name: 'Gear order' }], products: [{ id: 16, name: 'Gear product' }] })
    show(<GlobalSearch />); await searchGear()
    expect(screen.queryByRole('button', { name: 'Gear order' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Gear product' })).not.toBeInTheDocument()
  })
  it('closes and clears search on Escape', async () => {
    show(<GlobalSearch />); await searchGear(); fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument(); expect(screen.getByRole('combobox')).toHaveValue('')
  })
})

function pendingSearch() {
 let resolve!: (value: typeof results) => void
 const promise = new Promise<typeof results>(done => { resolve = done })
 return { promise, resolve }
}
it('older search response cannot replace newer results', async () => {
 const old = pendingSearch(); const fresh = pendingSearch()
 api.query.mockImplementation((q:string) => q === 'older' ? old.promise : fresh.promise)
 show(<GlobalSearch />); const input = screen.getByRole('combobox'); fireEvent.focus(input)
 fireEvent.change(input, { target: { value: 'older' } })
 await waitFor(() => expect(api.query).toHaveBeenCalledWith('older'))
 fireEvent.change(input, { target: { value: 'newer' } })
 await waitFor(() => expect(api.query).toHaveBeenCalledWith('newer'))
 await act(async () => fresh.resolve({ ...results, models: [{ id: 90, name: 'Newer result' }] }))
 expect(screen.getByRole('button', { name: /Newer\s*result/ })).toBeInTheDocument()
 await act(async () => old.resolve({ ...results, models: [{ id: 91, name: 'Obsolete result' }] }))
 expect(screen.queryByRole('button', { name: /Obsolete\s*result/ })).not.toBeInTheDocument()
 expect(screen.getByRole('button', { name: /Newer\s*result/ })).toBeInTheDocument()
})
it('Escape invalidates a pending search before refocusing', async () => {
 const pending = pendingSearch(); api.query.mockReturnValue(pending.promise)
 show(<GlobalSearch />); const input = screen.getByRole('combobox'); fireEvent.focus(input)
 fireEvent.change(input, { target: { value: 'pending' } })
 await waitFor(() => expect(api.query).toHaveBeenCalledWith('pending'))
 fireEvent.keyDown(document, { key: 'Escape' })
 await act(async () => pending.resolve(results))
 fireEvent.focus(input)
 expect(input).toHaveValue(''); expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
})
it('entitlement change invalidates pending privileged search results', async () => {
 const pending = pendingSearch(); api.query.mockReturnValueOnce(pending.promise).mockImplementation(() => new Promise(() => {}))
 const view = render(<MemoryRouter><GlobalSearch /></MemoryRouter>)
 const input = screen.getByRole('combobox'); fireEvent.focus(input); fireEvent.change(input, { target: { value: 'pending' } })
 await waitFor(() => expect(api.query).toHaveBeenCalledWith('pending'))
 api.isPro = false; view.rerender(<MemoryRouter><GlobalSearch /></MemoryRouter>)
 await act(async () => pending.resolve({ ...results, orders: [{ id: 92, name: 'Obsolete order' }] } as typeof results))
 expect(screen.queryByRole('button', { name: 'Obsolete order' })).not.toBeInTheDocument()
 expect(screen.queryByRole('button', { name: /Gear.*printer/ })).not.toBeInTheDocument()
})
