import React from 'react'
import { cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import DispatchRecoveryPanel from './DispatchRecoveryPanel'
const mocks = vi.hoisted(() => ({ inspect: vi.fn() }))
vi.mock('../../permissions', () => ({ canDo: () => false }))
vi.mock('../../api', () => ({ printers: { dispatchRecovery: mocks.inspect } }))
const uncertain = { state: 'uncertain', job_id: 53, job: { status: 'printing', held: true }, receipt_present: true, observed_print: { id: 54, status: 'completed' }, release_available: false, guidance: 'Idle alone is insufficient.' }
const clients: QueryClient[] = []
function mount(id = 59) { const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } }); clients.push(client); return render(<QueryClientProvider client={client}><DispatchRecoveryPanel printerId={id}/></QueryClientProvider>) }
beforeEach(() => { mocks.inspect.mockReset(); mocks.inspect.mockResolvedValue(uncertain) })
afterEach(() => { cleanup(); clients.forEach(c => c.clear()); clients.length = 0 })
it('shows retained uncertainty despite a completed observation and offers no release', async () => { mount(); expect(await screen.findByText(/The dispatch result is uncertain/)).toBeInTheDocument(); expect(screen.getByText(/54: completed/)).toHaveTextContent('54: completed'); expect(screen.getByText(/Recorded state: printing/)).toHaveTextContent('Held from scheduling'); expect(screen.queryByRole('button', { name: /release|retry a print/i })).not.toBeInTheDocument(); expect(mocks.inspect).toHaveBeenCalledWith(59) })
it('reports no fence without claiming success', async () => { mocks.inspect.mockResolvedValue({ state: 'no_retained_claim', release_available: false }); mount(); expect(await screen.findByText(/does not confirm a successful print/)).toBeInTheDocument() })
it('keeps missing observations and scoped job metadata unknown', async () => { mocks.inspect.mockResolvedValue({ ...uncertain, job: null, receipt_present: false, observed_print: null }); mount(); expect(await screen.findByText(/Do not infer completion/)).toBeInTheDocument(); expect(screen.getByText(/unavailable in this scope/)).toBeInTheDocument(); expect(screen.getByText('No admission receipt is linked.')).toBeInTheDocument() })
it('shows an error and refreshes actual evidence on request', async () => { mocks.inspect.mockRejectedValueOnce(new Error('synthetic')); mount(); expect(await screen.findByRole('alert')).toHaveTextContent('unavailable'); fireEvent.click(screen.getByRole('button', { name: 'Refresh evidence' })); expect(await screen.findByText(/The dispatch result is uncertain/)).toBeInTheDocument(); expect(mocks.inspect).toHaveBeenCalledTimes(2) })
it('reads the selected printer only', async () => { mount(60); await screen.findByText(/The dispatch result is uncertain/); expect(mocks.inspect).toHaveBeenCalledWith(60); expect(mocks.inspect).not.toHaveBeenCalledWith(59) })
it('renders Education status as evidence without inventing terminal truth', async () => { mocks.inspect.mockResolvedValue({ ...uncertain, education: { submission_id: 12, status: 'approved', reservation_state: 'reserved' } }); mount(); expect(await screen.findByText(/Education submission 12: approved/)).toHaveTextContent('reserved') })
