import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import TermRetention from './TermRetention'
const api = vi.hoisted(() => ({ list: vi.fn(), create: vi.fn(), preview: vi.fn(), purge: vi.fn(), anonymize: vi.fn(), remove: vi.fn(), resumeErasure: vi.fn() }))
const pending = vi.hoisted(() => [] as Array<{ user_id: number; username: string }>)
const term = { id: 'term', name: 'Spring', starts_at: '2020-01-01', ends_at: '2020-02-01' }
vi.mock('../../api/termRetention', () => ({ termRetention: api }))
vi.mock('@tanstack/react-query', () => ({ useQuery: () => ({ data: { items: [term], pending_erasures: pending } }), useQueryClient: () => ({ invalidateQueries: vi.fn() }) }))
beforeEach(() => {
  vi.clearAllMocks()
  pending.length = 0
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  api.preview.mockResolvedValue({ fingerprint: 'abc', due: { files: true }, eligible_student_ids: [1], eligible_students: [{ id: 1, username: 'student' }], items: [{ submission_id: 14, user_id: 1, kinds: ['files'], blocked_reason: null }] })
  api.purge.mockResolvedValue({ results: [{ state: 'done' }] })
  api.anonymize.mockResolvedValue({ results: [{ state: 'anonymized' }] })
})
afterEach(() => { cleanup(); vi.restoreAllMocks() })
it('requires a preview and sends the reviewed fingerprint for purge', async () => {
  render(<TermRetention audience="school-admin" centers={[]} />)
  expect(screen.queryByRole('button', { name: 'Purge due content' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Preview Spring' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Purge due content' }))
  await waitFor(() => expect(api.purge).toHaveBeenCalledWith('term', 'abc', 0))
  expect(await screen.findByRole('status')).toHaveTextContent('1 completed, 0 blocked')
})
it('shows blocked work and disables destructive actions', async () => {
  api.preview.mockResolvedValue({ fingerprint: 'abc', due: {}, eligible_student_ids: [], eligible_students: [], items: [{ submission_id: 14, kinds: ['files'], blocked_reason: 'work_not_terminal' }] })
  render(<TermRetention audience="school-admin" centers={[]} />)
  fireEvent.click(screen.getByRole('button', { name: 'Preview Spring' }))
  expect(await screen.findByText(/Blocked — work not terminal/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Purge due content' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Anonymize selected students' })).toBeDisabled()
})
it('anonymizes only selected accounts using the dry run', async () => {
  render(<TermRetention audience="school-admin" centers={[]} />)
  fireEvent.click(screen.getByRole('button', { name: 'Preview Spring' }))
  fireEvent.click(await screen.findByLabelText('student (account 1)'))
  fireEvent.click(screen.getByRole('button', { name: 'Anonymize selected students' }))
  await waitFor(() => expect(api.anonymize).toHaveBeenCalledWith('term', 'abc', [1], 0))
})
it('surfaces stale-preview failures without claiming completion', async () => {
  api.purge.mockRejectedValue(new Error('Dry run changed; preview again'))
  render(<TermRetention audience="school-admin" centers={[]} />)
  fireEvent.click(screen.getByRole('button', { name: 'Preview Spring' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Purge due content' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Dry run changed')
})
it('canceling confirmation sends no purge request', async () => {
  vi.mocked(window.confirm).mockReturnValue(false)
  render(<TermRetention audience="school-admin" centers={[]} />)
  fireEvent.click(screen.getByRole('button', { name: 'Preview Spring' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Purge due content' }))
  expect(api.purge).not.toHaveBeenCalled()
})
it('deletes an unused policy after confirmation', async () => {
  api.remove.mockResolvedValue({ status: 'deleted' })
  render(<TermRetention audience="school-admin" centers={[]} />)
  fireEvent.click(screen.getByRole('button', { name: 'Delete policy' }))
  await waitFor(() => expect(api.remove).toHaveBeenCalledWith('term'))
})
it('loads the returned cursor and displays blocked reasons', async () => {
  api.purge.mockResolvedValue({ next_after_id: 14, results: [{ submission_id: 14, kind: 'files', state: 'blocked', reason: 'database_error' }] })
  render(<TermRetention audience="school-admin" centers={[]} />)
  fireEvent.click(screen.getByRole('button', { name: 'Preview Spring' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Purge due content' }))
  await waitFor(() => expect(api.preview).toHaveBeenLastCalledWith('term', 14))
  expect(await screen.findByRole('status')).toHaveTextContent('database_error')
})

it.each(['anonymized', 'erasure_incomplete'])('resumes pending erasure and reports %s', async state => {
  pending.push({ user_id: 1, username: '[deleted-1]' })
  api.resumeErasure.mockResolvedValue({ user_id: 1, state })
  render(<TermRetention audience="school-admin" centers={[]} />)
  fireEvent.click(screen.getByRole('button', { name: 'Resume erasure for account 1' }))
  await waitFor(() => expect(api.resumeErasure).toHaveBeenCalledWith(1))
  expect(await screen.findByRole('status')).toHaveTextContent(state === 'anonymized' ? 'erasure completed' : 'erasure_incomplete')
})

it('reports interrupted graduation as erasure_incomplete', async () => {
  api.anonymize.mockResolvedValue({ results: [{ user_id: 1, state: 'erasure_incomplete' }] })
  render(<TermRetention audience="school-admin" centers={[]} />)
  fireEvent.click(screen.getByRole('button', { name: 'Preview Spring' }))
  fireEvent.click(await screen.findByLabelText('student (account 1)'))
  fireEvent.click(screen.getByRole('button', { name: 'Anonymize selected students' }))
  expect(await screen.findByRole('status')).toHaveTextContent('0 anonymized, 1 erasure_incomplete')
})

it('reports blocked graduation with its reason', async () => {
  api.anonymize.mockResolvedValue({ results: [{ user_id: 1, state: 'blocked', reason: 'erasure_failed' }] })
  render(<TermRetention audience="school-admin" centers={[]} />)
  fireEvent.click(screen.getByRole('button', { name: 'Preview Spring' }))
  fireEvent.click(await screen.findByLabelText('student (account 1)'))
  fireEvent.click(screen.getByRole('button', { name: 'Anonymize selected students' }))
  expect(await screen.findByRole('status')).toHaveTextContent('0 anonymized, 0 erasure_incomplete, 1 blocked')
  expect(screen.getByRole('status')).toHaveTextContent('Account 1: erasure_failed')
})

it('reports a blocked resume without claiming incomplete erasure', async () => {
  pending.push({ user_id: 1, username: '[deleted-1]' })
  api.resumeErasure.mockResolvedValue({ user_id: 1, state: 'blocked', reason: 'no_pending_erasure' })
  render(<TermRetention audience="school-admin" centers={[]} />)
  fireEvent.click(screen.getByRole('button', { name: 'Resume erasure for account 1' }))
  expect(await screen.findByRole('status')).toHaveTextContent('blocked (no_pending_erasure)')
  expect(screen.getByRole('status')).not.toHaveTextContent('erasure_incomplete')
})
