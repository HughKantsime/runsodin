import { render, fireEvent, screen, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, test, vi } from 'vitest'
import OutcomeCorrection from './OutcomeCorrection'
import JobRow from './JobRow'
import { fetchAPI } from '../../api/client'
import { canDo } from '../../permissions'
vi.mock('../../api/client', () => ({ fetchAPI: vi.fn() }))
vi.mock('../../permissions', () => ({ canDo: vi.fn(() => true) }))
afterEach(() => { cleanup(); vi.clearAllMocks() })
function show(status='failed', printId?:number) {
  const onCorrected=vi.fn()
  render(<QueryClientProvider client={new QueryClient()}><OutcomeCorrection jobId={901} printId={printId} status={status} onCorrected={onCorrected} /></QueryClientProvider>)
  return onCorrected
}
test('job and archive corrections send the guarded terminal pair', async () => {
  vi.mocked(fetchAPI).mockResolvedValue({status:'cancelled'})
  const done=show('failed',902)
  fireEvent.click(screen.getByText('Correct outcome to cancelled'))
  await screen.findByText('Correct outcome to cancelled')
  expect(fetchAPI).toHaveBeenCalledWith('/jobs/901/outcome', {method:'POST',body:JSON.stringify({status:'cancelled',from_status:'failed',print_id:902})})
  expect(done).toHaveBeenCalledOnce()
})
test('refusal remains visible', async () => {
  vi.mocked(fetchAPI).mockRejectedValue(new Error('Job changed; reload'))
  show('cancelled')
  fireEvent.click(screen.getByText('Correct outcome to failed'))
  expect(await screen.findByRole('alert')).toHaveTextContent('Job changed; reload')
})
test('live jobs and users without jobs edit have no correction', () => {
  show('printing'); expect(screen.queryByRole('button')).not.toBeInTheDocument()
  cleanup(); vi.mocked(canDo).mockReturnValueOnce(false)
  show(); expect(screen.queryByRole('button')).not.toBeInTheDocument()
})

test('job page row exposes the correction for a failed job', () => {
  render(<QueryClientProvider client={new QueryClient()}><table><tbody><JobRow
    job={{id:901,status:'failed',item_name:'Synthetic failed print',priority:3,quantity:1,hold:false,is_locked:true}}
    onAction={vi.fn()} isSelected={false} onToggleSelect={vi.fn()}
  /></tbody></table></QueryClientProvider>)
  expect(screen.getByRole('button',{name:'Correct outcome to cancelled'})).toBeInTheDocument()
})
