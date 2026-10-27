import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, renderHook, act, waitFor, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useJobMutations } from './useJobMutations'
import { jobs } from '../api'
import toast from 'react-hot-toast'
import JobRow from '../components/jobs/JobRow'
vi.mock('../api', () => ({jobs:{dispatch:vi.fn()},approveJob:vi.fn(),rejectJob:vi.fn(),resubmitJob:vi.fn(),bulkOps:{}}))
vi.mock('../permissions', () => ({canDo:()=>true,getCurrentUser:()=>({role:'admin'})}))
vi.mock('react-hot-toast', () => ({default:Object.assign(vi.fn(),{success:vi.fn(),error:vi.fn()})}))
afterEach(() => {cleanup();vi.clearAllMocks()})
it('keeps an unconfirmed job fenced and shows uncertainty without success', async () => {
  vi.mocked(jobs.dispatch).mockResolvedValue({confirmation:'unconfirmed'})
  const client = new QueryClient({defaultOptions:{mutations:{retry:3}}})
  const wrapper = ({children}:any) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
  const {result} = renderHook(()=>useJobMutations(),{wrapper})
  act(()=>result.current.dispatchJob.mutate(53))
  await waitFor(()=>expect(result.current.dispatchFencedIds.has(53)).toBe(true))
  expect(toast.success).not.toHaveBeenCalled()
  expect(toast).toHaveBeenCalledWith(expect.stringContaining('unconfirmed'),expect.anything())
  render(<table><tbody><JobRow job={{id:53,item_name:'Test',status:'scheduled',printer_id:59,priority:3,quantity:1}} onAction={vi.fn()} isSelected={false} onToggleSelect={vi.fn()} dispatchFenced /></tbody></table>)
  expect(screen.getByRole('button',{name:'Dispatch requires verification'})).toBeDisabled()
  expect(jobs.dispatch).toHaveBeenCalledTimes(1)
  client.clear()
})
it('does not retry a rejected hardware POST even with global retries enabled', async () => {
  vi.mocked(jobs.dispatch).mockRejectedValue(new Error('Unavailable tray'))
  const client = new QueryClient({defaultOptions:{mutations:{retry:3,retryDelay:0}}})
  const wrapper = ({children}:any) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
  const {result} = renderHook(()=>useJobMutations(),{wrapper})
  act(()=>result.current.dispatchJob.mutate(53))
  await waitFor(()=>expect(result.current.dispatchJob.isError).toBe(true))
  expect(jobs.dispatch).toHaveBeenCalledTimes(1)
  client.clear()
})
it.each(['refused', 'needs_reconciliation'] as const)('shows the server nozzle mismatch for %s', async phase => {
  const message = 'nozzle_mismatch: left nozzle requires Standard; installed High Flow'
  vi.mocked(jobs.dispatch).mockResolvedValue({confirmation: 'unconfirmed', status: {job_id: 53, phase, message, retry_blocked: true, observation: null}})
  const client = new QueryClient()
  const wrapper = ({children}:any) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
  const {result} = renderHook(() => useJobMutations(), {wrapper})
  act(() => result.current.dispatchJob.mutate(53))
  await waitFor(() => expect(toast.error).toHaveBeenCalledWith(message))
  expect(toast.success).not.toHaveBeenCalled()
  client.clear()
})
