import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import TrainingPanel from './TrainingPanel'
import { educationTraining, type Training } from '../../api/educationTraining'
import type { EducationCostCenter } from '../../types'
vi.mock('../../api/educationTraining', () => ({ educationTraining: { list: vi.fn(), create: vi.fn(), update: vi.fn(), quiz: vi.fn(), roster: vi.fn(), assess: vi.fn() } }))
const training: Training = { id: 'training-1', revision: 1, edit_version: 1, can_edit: false, name: 'P1S safety', description: 'Stay clear of hot parts', scope_kind: 'printer_group', scope_value: '', quiz: [{ prompt: 'Touch hot nozzle?', choices: ['Yes', 'No'] }], pass_mark: 80, instructor_required: true, expiry_days: 365, active: true, status: 'required', record: null, can_manage: false }
const printers = [{ id: 9, name: 'P1S-09', machine_type: 'P1S', model: 'Bambu Lab P1S' }]
const centers = [{ id: 7, name: 'Engineering' }, { id: 8, name: 'Robotics' }] as EducationCostCenter[]
function mount() {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}><TrainingPanel centers={centers} audience="student" /></QueryClientProvider>)
}
beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(educationTraining.list).mockResolvedValue({ items: [training], can_manage: false })
  vi.mocked(educationTraining.quiz).mockResolvedValue({ status: 'instructor_required', quiz_score: 100 })
  vi.mocked(educationTraining.create).mockResolvedValue({})
  vi.mocked(educationTraining.update).mockResolvedValue({})
  vi.mocked(educationTraining.assess).mockResolvedValue({})
  vi.mocked(educationTraining.roster).mockResolvedValue({ items: [{ user_id: 1, username: 'Student', active: true, status: 'instructor_required', record: { quiz_score: 100, completed_at: null, expires_at: null, revoked_at: null } }] })
})
describe('student training', () => {
  it('requires every quiz answer and reports remaining instructor requirement', async () => {
    mount()
    const button = await screen.findByRole('button', { name: 'Submit quiz' })
    expect(button).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'New training' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('radio', { name: 'No' }))
    fireEvent.click(button)
    expect(await screen.findByRole('status')).toHaveTextContent('Score: 100%. Status: instructor required.')
    expect(educationTraining.quiz).toHaveBeenCalledWith(training, [1])
  })
  it('shows named refusals without claiming completion', async () => {
    vi.mocked(educationTraining.quiz).mockRejectedValue(new Error('Training is archived'))
    mount(); fireEvent.click(await screen.findByRole('radio', { name: 'No' }))
    fireEvent.click(screen.getByRole('button', { name: 'Submit quiz' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Training is archived')
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })
  it('switches class without reusing answers or roster data', async () => {
    mount(); fireEvent.click(await screen.findByRole('radio', { name: 'No' }))
    fireEvent.change(screen.getByLabelText('Training class'), { target: { value: '8' } })
    await waitFor(() => expect(educationTraining.list).toHaveBeenCalledWith(8))
    expect(await screen.findByRole('button', { name: 'Submit quiz' })).toBeDisabled()
  })
  it('completed training shows status and expiry and cannot be retaken', async () => {
    vi.mocked(educationTraining.list).mockResolvedValue({ can_manage: false, items: [{ ...training, status: 'complete', record: { completed_at: '2026-01-01', expires_at: '2027-01-01', quiz_score: 100, revoked_at: null } }] })
    mount(); expect(await screen.findByText(/Status: complete/)).toHaveTextContent('Expires')
    expect(screen.queryByRole('button', { name: 'Submit quiz' })).not.toBeInTheDocument()
  })
})
describe('instructor training', () => {
  beforeEach(() => vi.mocked(educationTraining.list).mockResolvedValue({ items: [{ ...training, can_manage: true, can_edit: true, quiz: [{ ...training.quiz[0], correct: 1 }] }], can_manage: true, printers, school_printers: printers, can_manage_school_wide: true }))
  it('creates a training with machine scope, quiz, instructor signoff and expiry', async () => {
    mount(); fireEvent.click(await screen.findByRole('button', { name: 'New training' }))
    fireEvent.change(screen.getByLabelText('Training name'), { target: { value: 'New safety' } })
    fireEvent.change(screen.getByLabelText('Printer scope'), { target: { value: 'machine_type' } })
    fireEvent.change(screen.getByLabelText('Exact machine type (e.g. P1S)'), { target: { value: 'P1S' } })
    fireEvent.change(screen.getByLabelText(/Expires after days/), { target: { value: '365' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add quiz question' }))
    fireEvent.change(screen.getByLabelText('Question text'), { target: { value: 'Hot?' } })
    fireEvent.change(screen.getByLabelText('Choice 1'), { target: { value: 'Yes' } })
    fireEvent.change(screen.getByLabelText('Choice 2'), { target: { value: 'No' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save training' }))
    await waitFor(() => expect(educationTraining.create).toHaveBeenCalledWith(7, expect.objectContaining({ name: 'New safety', scope_kind: 'machine_type', scope_value: 'P1S', expiry_days: 365, instructor_required: true, quiz: [{ prompt: 'Hot?', choices: ['Yes', 'No'], correct: 0 }] })))
  })
  it('loads roster and sends signoff and revocation actions', async () => {
    mount(); fireEvent.click(await screen.findByRole('button', { name: 'View student sign-offs' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Sign off Student' }))
    await waitFor(() => expect(educationTraining.assess).toHaveBeenCalledWith(expect.objectContaining({ id: 'training-1', revision: 1 }), 1, 'signoff'))
    fireEvent.click(screen.getByRole('button', { name: 'Revoke Student' }))
    await waitFor(() => expect(educationTraining.assess).toHaveBeenCalledWith(expect.anything(), 1, 'revoke'))
  })
  it('updates definitions and explains invalidation', async () => {
    mount(); fireEvent.click(await screen.findByRole('button', { name: 'Edit training' }))
    expect(screen.queryByText(/invalidates existing completions/)).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Training name'), { target: { value: 'Revised safety' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save training' }))
    await waitFor(() => expect(educationTraining.update).toHaveBeenCalledWith(expect.objectContaining({ revision: 1 }), expect.objectContaining({ name: 'Revised safety' })))
  })
  it('archives a requirement through the revision guarded update', async () => {
    mount(); fireEvent.click(await screen.findByRole('button', { name: 'Archive requirement' }))
    await waitFor(() => expect(educationTraining.update).toHaveBeenCalledWith(expect.anything(), expect.objectContaining({ active: false })))
  })
})
it('allows an instructor to revoke retained evidence for an inactive student', async () => {
  vi.mocked(educationTraining.list).mockResolvedValue({ items: [{ ...training, can_manage: true, can_edit: true }], can_manage: true })
  vi.mocked(educationTraining.roster).mockResolvedValue({ items: [{ user_id: 1, username: 'Former student', active: false, status: 'complete', record: { quiz_score: 100, completed_at: '2026-01-01', expires_at: null, revoked_at: null } }] })
  mount(); fireEvent.click(await screen.findByRole('button', { name: 'View student sign-offs' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Revoke Former student' }))
  await waitFor(() => expect(educationTraining.assess).toHaveBeenCalledWith(expect.anything(), 1, 'revoke'))
  expect(screen.queryByRole('button', { name: 'Sign off Former student' })).not.toBeInTheDocument()
})

it('warns for requirement changes and blocks a scope with no matching printers', async () => {
  vi.mocked(educationTraining.list).mockResolvedValue({ items: [{ ...training, can_manage: true, can_edit: true, quiz: [{ ...training.quiz[0], correct: 1 }] }], can_manage: true, printers })
  mount(); fireEvent.click(await screen.findByRole('button', { name: 'Edit training' }))
  expect(screen.queryByText(/invalidates existing completions/)).not.toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('Pass mark (%)'), { target: { value: '90' } })
  expect(screen.getByText(/invalidates existing completions/)).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('Printer scope'), { target: { value: 'model' } })
  fireEvent.change(screen.getByLabelText('Exact model name'), { target: { value: 'Unknown printer' } })
  expect(screen.getByRole('button', { name: 'Save training' })).toBeDisabled()
  expect(screen.getByText(/choose a scope that matches/)).toBeInTheDocument()
})

it('shows school-wide scope and sends it in the definition', async () => {
  vi.mocked(educationTraining.list).mockResolvedValue({ items: [], can_manage: true, printers: [], school_printers: printers, can_manage_school_wide: true })
  mount(); fireEvent.click(await screen.findByRole('button', { name: 'New training' }))
  fireEvent.change(screen.getByLabelText('Training name'), { target: { value: 'School safety' } })
  expect(screen.getByRole('button', { name: 'Save training' })).toBeDisabled()
  fireEvent.click(screen.getByLabelText('School-wide training'))
  expect(screen.getByText('Matched printers: P1S-09')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Save training' }))
  await waitFor(() => expect(educationTraining.create).toHaveBeenCalledWith(7, expect.objectContaining({ school_wide: true })))
})

it('shows empty-scope warnings and quiz attempt counts in the roster', async () => {
  vi.mocked(educationTraining.list).mockResolvedValue({ items: [{ ...training, can_manage: true, can_edit: true, matched_printers: [] }], can_manage: true, printers })
  vi.mocked(educationTraining.roster).mockResolvedValue({ items: [{ user_id: 1, username: 'Student', active: true, status: 'quiz_required', record: { quiz_score: 0, completed_at: null, expires_at: null, revoked_at: null, quiz_attempts: 3, cooldown_until: '2026-11-05T20:15:00Z' } }] })
  mount(); expect(await screen.findByRole('alert')).toHaveTextContent('no longer matches a printer')
  fireEvent.click(screen.getByRole('button', { name: 'View student sign-offs' }))
  expect(await screen.findByText(/Quiz attempts: 3/)).toHaveTextContent('Cooldown until')
})

it('hides the school-wide toggle from class managers', async () => {
  vi.mocked(educationTraining.list).mockResolvedValue({ items: [], can_manage: true, printers })
  mount(); fireEvent.click(await screen.findByRole('button', { name: 'New training' }))
  expect(screen.queryByLabelText('School-wide training')).not.toBeInTheDocument()
})

it('keeps instructor actions but hides school-wide editing from class managers', async () => {
  vi.mocked(educationTraining.list).mockResolvedValue({ items: [{ ...training, school_wide: true, can_manage: true, can_edit: false }], can_manage: true, printers })
  mount(); await screen.findByRole('button', { name: 'View student sign-offs' })
  expect(screen.queryByRole('button', { name: 'Edit training' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Archive requirement' })).not.toBeInTheDocument()
})

it('hides quiz retakes while awaiting sign-off', async () => {
  vi.mocked(educationTraining.list).mockResolvedValue({ items: [{ ...training, status: 'instructor_required' }], can_manage: false })
  mount(); await screen.findByText('Status: instructor required')
  expect(screen.queryByRole('button', { name: 'Submit quiz' })).not.toBeInTheDocument()
})
