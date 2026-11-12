import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import OnboardingTour from './OnboardingTour'

const h = vi.hoisted(() => ({ list: vi.fn(), user: { id: 7, role: 'admin' } as { id: number; role: string } | null }))
vi.mock('../../api', () => ({ printers: { list: h.list } }))
vi.mock('../../permissions', () => ({ getCurrentUser: () => h.user }))

function show() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/']}><OnboardingTour /></MemoryRouter></QueryClientProvider>)
}
async function settle() {
  await waitFor(() => expect(h.list).toHaveBeenCalled())
  await act(async () => { vi.advanceTimersByTime(2000) })
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  localStorage.clear()
  h.list.mockReset().mockResolvedValue([])
  h.user = { id: 7, role: 'admin' }
})
afterEach(() => { cleanup(); vi.useRealTimers() })

describe('OnboardingTour visibility', () => {
  it('shows on an install with no printers', async () => {
    show(); await settle()
    expect(screen.getByText(/Getting Started/)).toBeInTheDocument()
  })

  it('does not show on an install that already has printers', async () => {
    h.list.mockResolvedValue([{ id: 1, name: 'P1' }])
    show(); await settle()
    expect(screen.queryByText(/Getting Started/)).not.toBeInTheDocument()
  })

  it('does not show when the printer list cannot be read', async () => {
    h.list.mockRejectedValue(new Error('403'))
    show(); await settle()
    expect(screen.queryByText(/Getting Started/)).not.toBeInTheDocument()
  })

  it('stays dismissed for that user after Skip, but not for another user', async () => {
    show(); await settle()
    fireEvent.click(screen.getByTitle('Skip tour'))
    expect(localStorage.getItem('odin_onboarding_complete:7')).toBe('true')
    cleanup()
    show(); await settle()
    expect(screen.queryByText(/Getting Started/)).not.toBeInTheDocument()
    cleanup()
    h.user = { id: 8, role: 'operator' }
    show(); await settle()
    expect(screen.getByText(/Getting Started/)).toBeInTheDocument()
  })

  it('honours the old browser-wide completion flag', async () => {
    localStorage.setItem('odin_onboarding_complete', 'true')
    show()
    await act(async () => { vi.advanceTimersByTime(2000) })
    expect(screen.queryByText(/Getting Started/)).not.toBeInTheDocument()
    expect(h.list).not.toHaveBeenCalled()
  })

  it('does not show before the user is known', async () => {
    h.user = null
    show()
    await act(async () => { vi.advanceTimersByTime(2000) })
    expect(screen.queryByText(/Getting Started/)).not.toBeInTheDocument()
  })
})
