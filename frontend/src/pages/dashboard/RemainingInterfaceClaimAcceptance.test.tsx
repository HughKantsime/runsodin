import React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation } from 'react-router-dom'
import Dashboard from './Dashboard'
import { I18nProvider, LANGUAGES, useTranslation } from '../../contexts/I18nContext'
import en from '../../i18n/en.json'
import de from '../../i18n/de.json'
import ja from '../../i18n/ja.json'
import es from '../../i18n/es.json'
import itCatalog from '../../i18n/it.json'

const api = vi.hoisted(() => ({ language: vi.fn() }))
vi.mock('../../api', () => ({ fetchAPI: api.language, stats: { get: vi.fn() }, jobs: { list: vi.fn() }, printers: { list: vi.fn() }, printJobs: { list: vi.fn() }, alerts: { summary: vi.fn() }, maintenance: { getStatus: vi.fn() } }))
vi.mock('../../permissions', () => ({ canDo: () => true }))
const catalogs = { en, de, ja, es, it: itCatalog }
const clients: QueryClient[] = []
function LocaleProbe() {
  const { locale, t, setLocale } = useTranslation()
  return <><output data-testid="locale">{locale}</output><output data-testid="translated">{t('nav.dashboard')}</output><output data-testid="fallback">{t('alerts.mark_all_read')}</output><output data-testid="unknown">{t('missing-test-key', 'Supplied default')}</output><output data-testid="raw">{t('missing-test-key')}</output><button onClick={() => setLocale('it')}>Change to Italian</button><button onClick={() => setLocale('invalid')}>Unsupported locale</button></>
}
function Route() { const l = useLocation(); return <output data-testid="route">{l.pathname}</output> }
function showDashboard(tasks: any[] = []) {
  const c = new QueryClient({ defaultOptions: { queries: { enabled: false, retry: false } } }); clients.push(c)
  c.setQueryData(['stats'], { jobs: {} }); c.setQueryData(['jobs', 'active'], []); c.setQueryData(['print-jobs'], [])
  c.setQueryData(['printers'], [{ id: 1, name: 'Fixture Alpha', last_seen: new Date().toISOString(), gcode_state: 'IDLE', filament_slots: [] }, { id: 2, name: 'Fixture Beta', last_seen: new Date().toISOString(), gcode_state: 'IDLE', filament_slots: [] }])
  c.setQueryData(['maintenance-status'], [{ printer_id: 1, printer_name: 'Fixture Alpha', tasks }])
  return render(<QueryClientProvider client={c}><MemoryRouter><I18nProvider><Route /><Dashboard /></I18nProvider></MemoryRouter></QueryClientProvider>)
}
const due = { task_name: 'Lubricate rails', status: 'overdue', progress_percent: 130, hours_since_service: 120, days_since_service: 30 }
beforeEach(() => { localStorage.clear(); vi.clearAllMocks(); api.language.mockResolvedValue({}) })
afterEach(() => { cleanup(); clients.forEach(c => c.clear()); clients.length = 0; vi.restoreAllMocks() })

it('F16-16.5-01 exposes the advertised five locale codes and 181 English keys', () => {
  expect(LANGUAGES.map(l => l.code).sort()).toEqual(['de','en','es','it','ja'])
  expect(Object.keys(en)).toHaveLength(181)
  for (const catalog of Object.values(catalogs)) {
    expect(Object.values(catalog).every(value => typeof value === 'string' && value.length > 0)).toBe(true)
    expect(Object.keys(catalog).sort()).toEqual(Object.keys(en).sort())
  }
})
it.each(['en','de','ja','es','it'] as const)('F16-16.5-01 actual provider translates a populated key for %s', locale => {
  localStorage.setItem('odin-locale', locale); render(<I18nProvider><LocaleProbe /></I18nProvider>)
  expect(screen.getByTestId('translated')).toHaveTextContent(catalogs[locale]['nav.dashboard'])
  expect(screen.getByTestId('unknown')).toHaveTextContent('Supplied default'); expect(screen.getByTestId('raw')).toHaveTextContent('missing-test-key')
})
it('F16-16.5-01 translates German keys, saves locale changes and rejects invalid locale', async () => {
  localStorage.setItem('odin-locale', 'de'); render(<I18nProvider><LocaleProbe /></I18nProvider>)
  expect(screen.getByTestId('fallback')).toHaveTextContent(de['alerts.mark_all_read'])
  fireEvent.click(screen.getByRole('button', { name: 'Change to Italian' }))
  expect(screen.getByTestId('translated')).toHaveTextContent(itCatalog['nav.dashboard']); expect(localStorage.getItem('odin-locale')).toBe('it')
  await waitFor(() => expect(api.language).toHaveBeenCalledWith('/settings/language', { method: 'PUT', body: JSON.stringify({ language: 'it' }) }))
  fireEvent.click(screen.getByRole('button', { name: 'Unsupported locale' })); expect(screen.getByTestId('locale')).toHaveTextContent('it')
})
it('F16-16.5-01 initializes from server locale preference', async () => {
  api.language.mockResolvedValue({ language: 'ja' }); render(<I18nProvider><LocaleProbe /></I18nProvider>)
  await waitFor(() => expect(screen.getByTestId('translated')).toHaveTextContent(ja['nav.dashboard']))
  expect(localStorage.getItem('odin-locale')).toBe('ja')
})
it.each(['de','ja','es','it'] as const)('F16-16.5-01 %s covers every advertised English key', locale => {
  expect(Object.keys(catalogs[locale]).sort()).toEqual(Object.keys(en).sort())
})
it.each(['de','ja','es','it'] as const)('F16-16.5-01 actual dashboard uses %s heading and stat translation', locale => {
  localStorage.setItem('odin-locale', locale); showDashboard()
  expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(catalogs[locale]['nav.dashboard'])
  expect(screen.getByText(catalogs[locale]['dashboard.completed_today'])).toBeInTheDocument()
})
it('F16-16.6-01 renders two actual dashboard printer cards with navigation', () => {
  const view = showDashboard(); const fleet = view.container.querySelector('[data-capture="fleet-grid"]')!
  expect(within(fleet as HTMLElement).getByRole('heading', { name: 'Fixture Alpha' })).toBeInTheDocument()
  expect(within(fleet as HTMLElement).getByRole('heading', { name: 'Fixture Beta' })).toBeInTheDocument()
  expect(fleet).toHaveClass('grid-cols-1', 'sm:grid-cols-2', 'lg:grid-cols-3', 'xl:grid-cols-4')
  fireEvent.click(screen.getByRole('heading', { name: 'Fixture Alpha' })); expect(screen.getByTestId('route')).toHaveTextContent('/printers')
})
it('F16-16.6-02 renders clamped progress, overdue badge, near-due task and maintenance navigation', () => {
  showDashboard([due, { ...due, task_name: 'Clean bed', status: 'ok', progress_percent: 80 }, { ...due, task_name: 'Hidden low-progress task', status: 'ok', progress_percent: 74 }])
  const heading = screen.getByRole('heading', { name: 'Maintenance Due' }); const widget = heading.parentElement!
  const bars = within(widget).getAllByRole('progressbar'); expect(bars.map(b => b.getAttribute('aria-valuenow'))).toEqual(['100','80'])
  expect(within(widget).getAllByText('OVERDUE')).toHaveLength(1); expect(within(widget).queryByText('Hidden low-progress task')).not.toBeInTheDocument()
  expect(within(widget).getAllByText('30d since service · 120h printed')).toHaveLength(2)
  fireEvent.click(within(widget).getByText('Lubricate rails')); expect(screen.getByTestId('route')).toHaveTextContent('/maintenance')
})
it('F16-16.6-02 limits visible maintenance cards to six with remaining count', () => {
  showDashboard(Array.from({ length: 8 }, (_, i) => ({ ...due, task_name: `Task ${i}` })))
  const widget = screen.getByRole('heading', { name: 'Maintenance Due' }).parentElement!
  expect(within(widget).getAllByRole('progressbar')).toHaveLength(6); expect(within(widget).getByText('+2 more')).toBeInTheDocument()
})
it('F16-16.6-02 hides maintenance widget when no task requires attention', () => {
  showDashboard([{ ...due, status: 'ok', progress_percent: 74 }]); expect(screen.queryByRole('heading', { name: 'Maintenance Due' })).not.toBeInTheDocument()
})
