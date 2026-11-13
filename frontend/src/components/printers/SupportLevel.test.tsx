import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { SupportLevelBadge, ExperimentalOptIn } from './SupportLevel'
import PrinterModal from './PrinterModal'
import { SUPPORT_LEVELS } from '../../utils/supportLevels'
import { printerSupport } from '../../api/printerSupport'

const state = vi.hoisted(() => ({ user: { id: 1, username: 'owner', role: 'admin', group_id: null as number | null } }))
vi.mock('../../permissions', () => ({ canDo: () => true, getCurrentUser: () => state.user }))
vi.mock('../../api/printerSupport', () => ({ printerSupport: { setExperimentalActions: vi.fn() } }))

const WARNING = "Built from OctoPrint's public API and not yet verified on real hardware. Sending jobs and controls may not work as expected. Send printer diagnostics if something is wrong."
const octo = { id: 7, api_type: 'octoprint', support_level: 'experimental', experimental_actions_enabled: false }
const bambu = { id: 8, api_type: 'bambu', support_level: 'supported', experimental_actions_enabled: false }
const original = { ...SUPPORT_LEVELS }

beforeEach(() => {
  state.user = { id: 1, username: 'owner', role: 'admin', group_id: null }
  vi.mocked(printerSupport.setExperimentalActions).mockResolvedValue({ printer_id: 7, support_level: 'experimental', experimental_actions_enabled: true })
})
afterEach(() => {
  cleanup(); vi.clearAllMocks()
  for (const key of Object.keys(SUPPORT_LEVELS)) delete SUPPORT_LEVELS[key]
  Object.assign(SUPPORT_LEVELS, original)
})

function mount(printer: any) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><SupportLevelBadge printer={printer} /><ExperimentalOptIn printer={printer} /></QueryClientProvider>)
}

it('shows the badge and the warning only for an Experimental printer', () => {
  mount(bambu)
  expect(screen.queryByTestId('support-badge')).not.toBeInTheDocument()
  expect(screen.queryByTestId('experimental-opt-in')).not.toBeInTheDocument()
  cleanup()
  mount(octo)
  expect(screen.getByTestId('support-badge')).toHaveTextContent('Experimental')
  expect(screen.getByRole('note')).toHaveTextContent(WARNING)
  expect(screen.getByText(/Job sending and controls are off for this printer/)).toBeInTheDocument()
})

it('renders nothing for a Supported printer without a query client', () => {
  render(<><SupportLevelBadge printer={bambu} /><ExperimentalOptIn printer={bambu} /></>)
  expect(screen.queryByTestId('experimental-opt-in')).not.toBeInTheDocument()
})

it('lets a platform superadmin turn job sending and controls on', async () => {
  mount(octo)
  fireEvent.click(screen.getByRole('checkbox', { name: 'Allow job sending and controls on this Experimental printer' }))
  await waitFor(() => expect(printerSupport.setExperimentalActions).toHaveBeenCalledWith(7, true))
})

it('does not offer the toggle to an organization admin', () => {
  state.user = { ...state.user, group_id: 3 }
  mount(octo)
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
  expect(screen.getByText('Only a platform administrator can change this.')).toBeInTheDocument()
})

it('shows the server refusal', async () => {
  vi.mocked(printerSupport.setExperimentalActions).mockRejectedValue(new Error('Platform administrator required'))
  mount(octo)
  fireEvent.click(screen.getByRole('checkbox'))
  expect(await screen.findByRole('alert')).toHaveTextContent('Platform administrator required')
})

it('labels an Experimental type in the add-printer list and warns when it is chosen', () => {
  SUPPORT_LEVELS.moonraker = 'experimental' // table marked in the test only
  render(<PrinterModal isOpen onClose={vi.fn()} onSubmit={vi.fn()} printer={null} />)
  const option = screen.getByRole('option', { name: 'Klipper / Moonraker — Experimental' })
  expect(screen.getByRole('option', { name: 'Prusa (PrusaLink)' })).toBeInTheDocument()
  fireEvent.change(option.closest('select')!, { target: { value: 'moonraker' } })
  expect(screen.getByRole('note')).toHaveTextContent(/not yet verified on real hardware/)
})
