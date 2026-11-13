import React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import DispatchRecoveryPanel from './DispatchRecoveryPanel'
import { printers } from '../../api'
import { canDo } from '../../permissions'
vi.mock('../../api', () => ({ printers: { dispatchRecovery: vi.fn(), recoverDispatch: vi.fn(), answerRestartQuestion: vi.fn() } }))
vi.mock('../../permissions', () => ({ canDo: vi.fn() }))
const question = { id: 'a'.repeat(32), print_job_id: 241, raised_at: '2026-11-13T12:00:00Z', print_name: 'Bracket', reported_name: 'Bracket' }
const evidence = { attempt_handle: 'dr1.synthetic', state: 'uncertain', job_id: 238, release_available: false, restart_question: question,
  answer_restart_question: true, actions: { hold: true, reconcile_retain: true, release: false }, blockers: ['active_observation'] }
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><DispatchRecoveryPanel printerId={9} /></QueryClientProvider>)
}
beforeEach(() => {
  vi.mocked(canDo).mockReturnValue(true)
  vi.mocked(printers.dispatchRecovery).mockResolvedValue(evidence)
  vi.mocked(printers.answerRestartQuestion).mockResolvedValue({ outcome: 'same', print_job_id: 241 })
})
afterEach(() => { cleanup(); vi.resetAllMocks(); document.documentElement.removeAttribute('data-theme-family') })
it.each(['classic', 'foundry'])('shows the question and both answers in %s with phone wrapping', async family => {
  document.documentElement.dataset.themeFamily = family
  mount()
  const yes = await screen.findByRole('button', { name: 'Yes, same print: keep recording on it' })
  const no = screen.getByRole('button', { name: 'No, it is a new print' })
  expect(yes).toHaveClass('whitespace-normal', 'min-h-[44px]')
  expect(no.parentElement).toHaveClass('flex-col', 'sm:flex-row')
  expect(screen.getByRole('button', { name: 'Release fence' })).toBeDisabled()
  expect(printers.answerRestartQuestion).not.toHaveBeenCalled()
})
it.each([['Yes, same print: keep recording on it', 'same'], ['No, it is a new print', 'new']])('records %s once', async (label, answer) => {
  vi.mocked(printers.answerRestartQuestion).mockImplementation(() => new Promise(() => {}))
  mount()
  const button = await screen.findByRole('button', { name: label })
  fireEvent.click(button); fireEvent.click(button)
  await waitFor(() => expect(printers.answerRestartQuestion).toHaveBeenCalledExactlyOnceWith(9, question.id, answer))
  expect(button).toBeDisabled()
  expect(printers.recoverDispatch).not.toHaveBeenCalled()
})
it('requires the server and client recovery permission', async () => {
  vi.mocked(canDo).mockReturnValue(false)
  mount()
  await screen.findByRole('region', { name: 'Confirm print after restart' })
  expect(screen.queryByRole('button', { name: /Yes, same print/ })).toBeNull()
  expect(screen.getByText(/An authorized operator or administrator can answer/)).toBeTruthy()
})
it('refreshes after an uncertain response without retrying the answer', async () => {
  vi.mocked(printers.answerRestartQuestion).mockRejectedValue(new Error('timeout'))
  mount()
  fireEvent.click(await screen.findByRole('button', { name: /Yes, same print/ }))
  await screen.findByText(/Your answer could not be confirmed/)
  await waitFor(() => expect(printers.dispatchRecovery).toHaveBeenCalledTimes(2))
  expect(printers.answerRestartQuestion).toHaveBeenCalledTimes(1)
})
it('keeps the answer notice after the question disappears', async () => {
  vi.mocked(printers.dispatchRecovery).mockResolvedValueOnce(evidence).mockResolvedValue({ ...evidence, restart_question: undefined })
  mount()
  fireEvent.click(await screen.findByRole('button', { name: /Yes, same print/ }))
  await waitFor(() => expect(screen.queryByRole('region', { name: 'Confirm print after restart' })).toBeNull())
  expect(screen.getByText('Your answer was recorded.')).toBeVisible()
})
it('explains linked job and later restart behavior', async () => {
  mount()
  await screen.findByRole('region', { name: 'Confirm print after restart' })
  expect(screen.getByText(/linked scheduled job stays/)).toBeVisible()
  expect(screen.getByText(/another restart/)).toBeVisible()
})
it.each(['classic', 'foundry'])('scrolls an alert link to recovery in %s', async family => {
  document.documentElement.dataset.themeFamily = family
  window.location.hash = '#dispatch-recovery'
  const scroll = vi.fn()
  Element.prototype.scrollIntoView = scroll
  mount()
  await screen.findByRole('region', { name: 'Confirm print after restart' })
  expect(scroll).toHaveBeenCalled()
  window.location.hash = ''
})

it('operators answer using their own permission without claim controls', async () => {
 vi.mocked(canDo).mockImplementation(action => action === 'printers.answer_restart_question')
 mount()
 expect(await screen.findByRole('button', { name: /Yes, same print/ })).toBeVisible()
 expect(screen.queryByRole('button', { name: 'Release fence' })).toBeNull()
 expect(screen.queryByRole('button', { name: 'Record decision and retain fence' })).toBeNull()
})
it('shows investigation records and repair instructions without answer buttons', async () => {
 vi.mocked(printers.dispatchRecovery).mockResolvedValue({ ...evidence, restart_question: { ...question, state: 'investigation', candidate_ids: [241, 242], message: 'Review records 241, 242 and close the records that are no longer running.' } } as any)
 mount()
 expect(await screen.findByText(/Review records 241, 242/)).toBeVisible()
 expect(screen.queryByRole('button', { name: /Yes, same print/ })).toBeNull()
})
