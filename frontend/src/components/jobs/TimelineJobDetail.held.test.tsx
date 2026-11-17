import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import BlockDetail from './TimelineJobDetail'
import { applyTheme } from '../../hooks/themePreferences'

const api = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('../../api', () => ({ jobs: { get: api.get } }))
vi.mock('../shared/ControlledPart', () => ({ default: () => null }))
vi.mock('./JobAmsMapping', () => ({ default: () => null }))
vi.mock('./JobTimeline', () => ({ default: () => null }))
vi.mock('./JobCost', () => ({ default: () => null }))
afterEach(() => { cleanup(); applyTheme('dark', 'classic'); vi.clearAllMocks() })

it.each([
  ['foundry', true, 'Waits for you (held)'],
  ['foundry', false, 'scheduled'],
  ['classic', true, 'scheduled'],
] as const)('%s detail describes hold=%s accurately without changing Classic', async (theme, hold, label) => {
  applyTheme('dark', theme)
  api.get.mockResolvedValue({ id: 1, display_name: 'Held part', status: 'scheduled', hold })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><BlockDetail block={{ job_id: 1, printer_name: 'Press', start: '2026-11-04T12:00:00Z', end: '2026-11-04T14:00:00Z' }} /></QueryClientProvider>)
  expect(await screen.findByText(label)).toBeInTheDocument()
  if (hold && theme === 'foundry') expect(screen.queryByText('scheduled')).toBeNull()
})
