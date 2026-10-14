import React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import GeneralTab from './GeneralTab'
import Sidebar from '../layout/Sidebar'
import { I18nProvider } from '../../contexts/I18nContext'
import de from '../../i18n/de.json'
import ja from '../../i18n/ja.json'
import es from '../../i18n/es.json'
import itCatalog from '../../i18n/it.json'
const boundary = vi.hoisted(() => ({ language: 'en', role: 'admin', group_id: null as number | null, request: vi.fn() }))
vi.mock('../../permissions', () => ({ getCurrentUser: () => ({ id: 1, role: boundary.role, group_id: boundary.group_id }), canAccessPage: () => true, clearSensitiveBrowserState: vi.fn() }))
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ tier: 'pro', isPro: true, isEducation: false, hasFeature: () => true }) }))
vi.mock('../../api', async importOriginal => ({ ...await importOriginal(), fetchAPI: boundary.request, pricingConfig: { get: async () => ({}) } }))
const catalogs = { de, ja, es, it: itCatalog }
const clients: QueryClient[] = []
function show() {
  const client = new QueryClient({ defaultOptions: { queries: { enabled: false, retry: false } } }); clients.push(client)
  return render(<QueryClientProvider client={client}><MemoryRouter><I18nProvider><Sidebar mobileOpen={false} onMobileClose={() => {}} /><GeneralTab /></I18nProvider></MemoryRouter></QueryClientProvider>)
}
beforeEach(() => {
  vi.stubGlobal('__APP_VERSION__', 'test'); localStorage.clear(); boundary.language = 'en'; boundary.role = 'admin'; boundary.group_id = null
  boundary.request.mockReset().mockImplementation(async (url, options) => {
    if (url === '/settings/language') {
      if (options?.method === 'PUT') boundary.language = JSON.parse(options.body).language
      return { language: boundary.language }
    }
    return {}
  })
})
afterEach(() => { cleanup(); clients.forEach(client => client.clear()); clients.length = 0 })
it.each(['de', 'ja', 'es', 'it'] as const)('real %s picker translates sidebar and persists through provider remount', async locale => {
  const view = show()
  const picker = await screen.findByRole('combobox', { name: 'Language' })
  expect(picker).toBeEnabled()
  fireEvent.change(picker, { target: { value: locale } })
  await waitFor(() => expect(boundary.request).toHaveBeenCalledWith('/settings/language', { method: 'PUT', body: JSON.stringify({ language: locale }) }))
  await waitFor(() => expect(screen.getByRole('combobox', { name: catalogs[locale]['settings.language'] })).toBeEnabled())
  expect(screen.getByRole('link', { name: catalogs[locale]['nav.dashboard'] })).toBeInTheDocument()
  expect(screen.getByText(catalogs[locale]['settings.language_desc'])).toBeInTheDocument()
  view.unmount(); localStorage.clear(); show()
  await waitFor(() => expect(screen.getByRole('combobox', { name: catalogs[locale]['settings.language'] })).toHaveValue(locale))
  expect(screen.getByRole('link', { name: catalogs[locale]['nav.printers'] })).toBeInTheDocument()
})
it.each([['viewer', null], ['operator', null], ['admin', 9]] as const)('%s with group %s cannot alter instance language', (role, group) => {
  boundary.role = role; boundary.group_id = group; show()
  expect(screen.getByRole('combobox', { name: 'Language' })).toBeDisabled()
  expect(boundary.request.mock.calls.some(([, options]) => options?.method === 'PUT')).toBe(false)
})
it('failed save restores previous locale and reports visible error', async () => {
  boundary.request.mockImplementation(async (_url, options) => { if (options?.method === 'PUT') throw new Error('Denied'); return { language: 'en' } })
  show(); fireEvent.change(screen.getByRole('combobox', { name: 'Language' }), { target: { value: 'de' } })
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Unable to save language preference'))
  expect(screen.getByRole('combobox', { name: 'Language' })).toHaveValue('en')
  expect(localStorage.getItem('odin-locale')).toBe('en')
})
