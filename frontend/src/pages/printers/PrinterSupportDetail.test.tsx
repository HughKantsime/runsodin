import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import PrinterDetail from './PrinterDetail'
import { printers } from '../../api'
import { printerSupport } from '../../api/printerSupport'

const state = vi.hoisted(() => ({ foundry: false, groupId: null as number | null, enabled: false }))
vi.mock('../../hooks/themePreferences', () => ({ useFoundry: () => state.foundry }))
vi.mock('../../permissions', () => ({ canDo: () => true, canAccessPage: () => false, getCurrentUser: () => ({ role: 'admin', group_id: state.groupId }) }))
vi.mock('../../components/printers/MaintenanceBanner', () => ({ default: () => null }))
vi.mock('../../components/printers/DispatchRecoveryPanel', () => ({ default: () => null }))
vi.mock('../../api/client', () => ({ fetchAPI: vi.fn().mockResolvedValue([]) }))
vi.mock('../../api/printerSupport', () => ({ printerSupport: { setExperimentalActions: vi.fn() } }))
vi.mock('../../api', () => ({
  printers: { get: vi.fn() },
  printerTelemetry: { get: vi.fn().mockResolvedValue([]) },
  printJobs: { list: vi.fn().mockResolvedValue([]) },
  jobs: { list: vi.fn().mockResolvedValue([]) },
}))

beforeEach(() => {
  state.enabled = false
  state.groupId = null
  vi.mocked(printers.get).mockImplementation(async () => ({
    id: 9, name: 'OctoPrint detail', api_type: 'octoprint', support_level: 'experimental',
    experimental_actions_enabled: state.enabled,
  }) as any)
  vi.mocked(printerSupport.setExperimentalActions).mockImplementation(async (_, enabled) => {
    state.enabled = enabled
    return { printer_id: 9, support_level: 'experimental', experimental_actions_enabled: enabled }
  })
})
afterEach(() => { cleanup(); vi.clearAllMocks() })

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/printers/9']}>
    <Routes><Route path="/printers/:id" element={<PrinterDetail />} /></Routes>
  </MemoryRouter></QueryClientProvider>)
}

it.each([false, true])('shows support and refreshes opt-in on direct detail navigation (foundry=%s)', async foundry => {
  state.foundry = foundry
  mount()
  expect(await screen.findByTestId('support-badge')).toHaveTextContent('Experimental')
  expect(screen.getByRole('note')).toHaveTextContent("Built from OctoPrint's public API and not yet verified on real hardware.")
  const toggle = screen.getByRole('checkbox', { name: 'Allow job sending and controls on this Experimental printer' })
  expect(toggle).not.toBeChecked()
  fireEvent.click(toggle)
  await waitFor(() => expect(printerSupport.setExperimentalActions).toHaveBeenCalledWith(9, true))
  await waitFor(() => expect(toggle).toBeChecked())
  expect(screen.getByText(/Job sending and controls are on for this printer/)).toBeInTheDocument()
})

it('shows the warning without an opt-in toggle to an organization admin on the detail page', async () => {
  state.foundry = false
  state.groupId = 3
  mount()
  expect(await screen.findByTestId('support-badge')).toBeInTheDocument()
  expect(screen.getByRole('note')).toBeInTheDocument()
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
})
