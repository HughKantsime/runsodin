import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import ClassesPanel from './ClassesPanel'
import StudentPortfolio from './StudentPortfolio'
import AssignmentForm from './AssignmentForm'
const api = vi.hoisted(() => ({ list: vi.fn(), create: vi.fn(), update: vi.fn(), roster: vi.fn(), assignments: vi.fn(), save: vi.fn(), portfolio: vi.fn(), link: vi.fn(), assess: vi.fn(), printers: vi.fn(), canDo: vi.fn() }))
vi.mock('../../api/educationClasses', () => ({ educationClasses: api }))
vi.mock('../../api', () => ({ education: { listCenterPrinters: api.printers } }))
vi.mock('../../permissions', () => ({ canDo: api.canDo }))
let client: QueryClient
const cls = { id: 'c1', cost_center_id: 7, title: 'Design', term: 'Fall', state: 'active', revision: 1, can_manage: true, can_submit: false }
const assignment = { id: 'a1', class_id: 'c1', title: 'Gear', brief: 'Make a gear', due_at: null, max_print_seconds: 1200, max_material_grams: 30, submission_quota: 2, allowed_materials: ['PLA'], auto_printer_ids: [], auto_print_seconds: null, auto_material_grams: null, state: 'active', revision: 1 }
const item = { submission_id: 14, item_name: 'My gear', student_username: 'Student', class_title: 'Design', term: 'Fall', assignment_title: 'Gear', assignment_id: 'a1', cost_center_id: 7, status: 'submitted', lifecycle_revision: 1, feedback: 'Good fit', grade: 'A', learning_status: 'complete', assessment_revision: 2, thumbnail_url: '/api/education/classes/portfolio/14/thumbnail', timelapse_ids: [8] }
beforeEach(() => {
  vi.clearAllMocks(); api.canDo.mockReturnValue(true)
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  api.list.mockResolvedValue({ items: [cls], next_offset: null }); api.roster.mockResolvedValue({ items: [{ user_id: 2, username: 'Teacher', role: 'manager' }] })
  api.assignments.mockResolvedValue({ items: [assignment], next_offset: null }); api.portfolio.mockResolvedValue({ items: [item], next_offset: null })
  api.printers.mockResolvedValue({ items: [{ printer_id: 9, name: 'Lab printer', state: 'active' }], next_cursor: null })
  api.save.mockResolvedValue(assignment); api.assess.mockResolvedValue({}); api.link.mockResolvedValue({ linked: true, auto_approval: 'manual_review' })
})
afterEach(() => { cleanup(); client.clear() })
function mount(element: React.ReactElement) { return render(<QueryClientProvider client={client}>{element}</QueryClientProvider>) }
async function choose() {
  await screen.findByRole('option', { name: 'Design · Fall' }); fireEvent.change(screen.getByLabelText('Select class'), { target: { value: 'c1' } })
  await screen.findByRole('option', { name: 'Gear' }); fireEvent.change(screen.getByLabelText('Select assignment'), { target: { value: 'a1' } })
}
it('shows instructor roster and saves feedback with the displayed revision', async () => {
  mount(<ClassesPanel audience="teacher" managedCenters={[]} />); await choose()
  expect(await screen.findByText('Teacher · Instructor')).toBeInTheDocument()
  expect(screen.getByText(/20 minutes/)).toBeInTheDocument()
  expect(await screen.findByLabelText('Grade')).toHaveValue('A')
  fireEvent.change(screen.getByLabelText('Grade'), { target: { value: 'B' } }); fireEvent.click(screen.getByRole('button', { name: 'Save assessment' }))
  await waitFor(() => expect(api.assess).toHaveBeenCalledWith('a1', item, expect.objectContaining({ grade: 'B' })))
})
it('shows private student feedback and timelapse without grading controls', async () => {
  mount(<StudentPortfolio audience="student" />)
  expect(await screen.findByText('Grade: A')).toBeInTheDocument(); expect(screen.getByText('Good fit')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Save assessment' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Watch timelapse 8' })); expect(document.querySelector('video')).toHaveAttribute('src', '/api/timelapses/8/stream')
  fireEvent.error(screen.getByAltText('Preview of My gear')); expect(screen.getByText('Thumbnail unavailable')).toBeInTheDocument()
})
it.each([false, true])('links own pending work and handles refusal: %s', async refusal => {
  api.list.mockResolvedValue({ items: [{ ...cls, can_manage: false, can_submit: true }], next_offset: null })
  api.portfolio.mockImplementation((id?: string) => Promise.resolve({ items: id ? [] : [{ ...item, assignment_id: null }], next_offset: null }))
  if (refusal) api.link.mockRejectedValue(new Error('Assignment submission quota reached'))
  mount(<ClassesPanel audience="student" managedCenters={[]} />); await choose(); await screen.findByRole('option', { name: 'My gear' })
  expect(screen.queryByRole('button', { name: 'New assignment' })).not.toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('Pending print for assignment'), { target: { value: '14' } }); fireEvent.click(screen.getByRole('button', { name: 'Link to assignment' }))
  if (refusal) expect(await screen.findByRole('alert')).toHaveTextContent('quota reached')
  else expect(await screen.findByText('Linked. Status: pending review.')).toBeInTheDocument()
  expect(api.link).toHaveBeenCalledWith('a1', 14, 1)
})
it.each([
  ['approved', 'Linked and approved for printing.'], ['pending', 'Linked and approved for printing.'],
  ['scheduled', 'Linked and approved for printing.'], ['printing', 'Linked and approved for printing.'],
  ['completed', 'Linked and approved for printing.'], ['failed', 'Linked. Status: failed.'],
  ['cancelled', 'Linked. Status: cancelled.'], ['rejected', 'Linked. Status: rejected.'],
  ['needs_revision', 'Linked. Status: needs revision.'], ['submitted', 'Linked. Status: pending review.'],
  ['manual_review', 'Linked. Status: pending review.'], ['review_unavailable', 'Linked. Status: pending review.'],
  ['pending_review', 'Linked. Status: pending review.'],
])('shows the correct link message for %s', async (status, message) => {
  api.list.mockResolvedValue({ items: [{ ...cls, can_manage: false, can_submit: true }], next_offset: null })
  api.portfolio.mockImplementation((id?: string) => Promise.resolve({ items: id ? [] : [{ ...item, assignment_id: null }], next_offset: null }))
  api.link.mockResolvedValue({ linked: true, auto_approval: status })
  mount(<ClassesPanel audience="student" managedCenters={[]} />); await choose()
  await screen.findByRole('option', { name: 'My gear' })
  fireEvent.change(screen.getByLabelText('Pending print for assignment'), { target: { value: '14' } })
  fireEvent.click(screen.getByRole('button', { name: 'Link to assignment' }))
  expect(await screen.findByRole('status')).toHaveTextContent(message)
})
it('saves explicit units and a complete auto-approval rule', async () => {
  const saved = vi.fn(); mount(<AssignmentForm classId="c1" centerId={7} audience="teacher" onSaved={saved} onCancel={() => {}} />)
  for (const [label, value] of [['Assignment title', 'Gear'], ['Maximum minutes per print', '20'], ['Submissions per student', '2']]) fireEvent.change(screen.getByLabelText(label), { target: { value } })
  fireEvent.click(screen.getByLabelText('Auto-approve eligible submissions'))
  fireEvent.change(screen.getByLabelText('Auto-approve at most minutes'), { target: { value: '10' } }); fireEvent.change(screen.getByLabelText('Auto-approve at most grams'), { target: { value: '20' } })
  fireEvent.click(await screen.findByLabelText('Lab printer')); fireEvent.click(screen.getByRole('button', { name: 'Save assignment' }))
  await waitFor(() => expect(saved).toHaveBeenCalled())
  expect(api.save).toHaveBeenCalledWith('c1', expect.objectContaining({ max_print_seconds: 1200, auto_print_seconds: 600, auto_material_grams: 20, auto_printer_ids: [9], submission_quota: 2 }), undefined)
})
it('handles portfolio errors and pagination', async () => {
  api.portfolio.mockRejectedValueOnce(new Error('Unavailable')); mount(<StudentPortfolio audience="student" />)
  expect(await screen.findByRole('alert')).toHaveTextContent('Unable to load portfolio')
  api.portfolio.mockResolvedValueOnce({ items: [item], next_offset: 30 }).mockResolvedValue({ items: [], next_offset: null })
  fireEvent.click(screen.getByText('Retry')); await screen.findByText('Grade: A'); fireEvent.click(screen.getByText('More submissions'))
  await waitFor(() => expect(api.portfolio).toHaveBeenCalledWith(undefined, 30))
})
it('hides writes when the permission matrix denies them', async () => {
  api.canDo.mockReturnValue(false); mount(<ClassesPanel audience="teacher" managedCenters={[]} />); await choose()
  expect(screen.queryByRole('button', { name: 'New assignment' })).not.toBeInTheDocument(); expect(screen.queryByRole('button', { name: 'Save assessment' })).not.toBeInTheDocument()
})
