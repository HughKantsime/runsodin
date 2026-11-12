// Mobile web pass, slice 3: the navigation drawer reports its open state and closes on backdrop tap and on navigation.
import { useState } from 'react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import Sidebar from './Sidebar'

vi.mock('../../api', () => ({
  printers: { list: vi.fn().mockResolvedValue([]) },
  stats: { get: vi.fn().mockResolvedValue({}) },
  pricingConfig: { get: vi.fn().mockResolvedValue({ ui_mode: 'advanced' }) },
  getEducationMode: vi.fn().mockResolvedValue({ enabled: false }),
}))
vi.mock('../../BrandingContext', () => ({ useBranding: () => ({ app_name: 'ODIN', app_subtitle: 'Farm' }) }))
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ isPro: true, isEducation: false }) }))
vi.mock('../../contexts/OrgContext', () => ({ useOrg: () => ({ orgId: null, orgs: [], isAdmin: false }) }))
vi.mock('../../permissions', () => ({ canAccessPage: () => true, clearSensitiveBrowserState: vi.fn() }))

beforeEach(() => { vi.stubGlobal('__APP_VERSION__', 'test') })
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

function Harness({ start = true }: { start?: boolean }) {
  const [open, setOpen] = useState(start)
  return <Sidebar mobileOpen={open} onMobileClose={() => setOpen(false)} />
}
function show(start = true) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><MemoryRouter><Harness start={start} /></MemoryRouter></QueryClientProvider>)
}
const drawer = () => screen.getByRole('navigation', { name: 'Main navigation' })

test('reports data-drawer-open for both states', () => {
  const view = show(true)
  expect(drawer()).toHaveAttribute('data-drawer-open', 'true')
  view.unmount()
  show(false)
  expect(drawer()).toHaveAttribute('data-drawer-open', 'false')
})

test('backdrop tap closes the drawer and the signal flips', () => {
  const { container } = show(true)
  fireEvent.click(container.querySelector('div.fixed.inset-0[aria-hidden="true"]')!)
  expect(drawer()).toHaveAttribute('data-drawer-open', 'false')
  expect(container.querySelector('div.fixed.inset-0[aria-hidden="true"]')).toBeNull()
})

test('choosing a page closes the drawer', () => {
  show(true)
  fireEvent.click(within(drawer()).getByRole('link', { name: 'Jobs' }))
  expect(drawer()).toHaveAttribute('data-drawer-open', 'false')
})

test('phone sheet classes are phone-only; the desktop classes are intact; the nav scrolls itself', () => {
  show(true)
  expect(drawer()).toHaveClass('max-sm:h-dvh', 'max-sm:inset-y-0')
  expect(drawer()).toHaveClass('fixed', 'md:relative', 'md:z-auto', 'w-64', 'translate-x-0')
  const nav = drawer().querySelector('nav')!
  expect(nav).toHaveClass('flex-1', 'overflow-y-auto', 'min-h-0', 'max-sm:overscroll-contain')
})
