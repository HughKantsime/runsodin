import React from 'react'
import { MemoryRouter } from 'react-router-dom'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import EducationWorkbench from './EducationWorkbench'
const state = vi.hoisted(() => ({
  principal: { id: 1, username: 'student', role: 'viewer', group_id: 7 } as any,
  capabilities: { education_enabled: true, student: false, manager: false, tenant_admin: false, student_cost_center_ids: [], managed_cost_center_ids: [] },
  licensed: true, error: false, loading: false, licenseError: false,
}))
vi.mock('../../contexts/OrgContext', () => ({ useOrg: () => ({ orgId: 7, orgs: [{ id: 7, name: 'School' }], switchOrg: vi.fn() }) }))
vi.mock('../../permissions', () => ({ getCurrentUser: () => ({ id: 1, username: 'student', role: 'viewer', group_id: 7 }) }))
vi.mock('../../api', () => ({ auth: { me: vi.fn() }, education: {}, getEducationMode: vi.fn() }))
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ isEducation: state.licensed, hasFeature: () => state.licensed, loading: state.loading, loadError: state.licenseError }) }))
vi.mock('@tanstack/react-query', () => ({
  useQueryClient: () => ({}),
  useQuery: ({ queryKey }: { queryKey: string[] }) => ({ data: queryKey[0] === 'auth' ? state.principal : queryKey[0] === 'education-mode' ? { enabled: true } : state.capabilities, isError: state.error, isLoading: false }),
  useInfiniteQuery: () => ({ data: { pages: [{ items: [] }] }, hasNextPage: false }),
}))
vi.mock('../../components/education/CostCenterManager', () => ({ default: () => null }))
vi.mock('../../components/education/ClassroomManager', () => ({ default: () => null }))
vi.mock('../../components/education/SubmissionQueue', () => ({ default: ({ reviewable }: { reviewable: boolean }) => <div data-testid="submission-queue">{reviewable ? 'review' : 'mine'}</div> }))
vi.mock('../../components/education/SubmissionUploadModal', () => ({ default: () => null, educationAudience: () => 'owned-student' }))
vi.mock('../../components/education/ReadinessPanel', () => ({ default: () => null }))
beforeEach(() => { state.principal = { id: 1, username: 'student', role: 'viewer', group_id: 7 }; Object.assign(state.capabilities, { education_enabled: true, student: false, manager: false, tenant_admin: false }); state.licensed = true; state.error = false; state.loading = false; state.licenseError = false })
afterEach(cleanup)
it('does not call an account without grants a student', () => { render(<MemoryRouter><EducationWorkbench /></MemoryRouter>); expect(screen.getByText('No classroom access')).toBeInTheDocument(); expect(screen.queryByText('Student')).not.toBeInTheDocument(); expect(screen.getByText(/An Operator needs Manager/)).toBeInTheDocument() })
it('shows dual submission and review access', () => { Object.assign(state.capabilities, { student: true, manager: true }); render(<MemoryRouter><EducationWorkbench /></MemoryRouter>); expect(screen.getByText('Manager + Student')).toBeInTheDocument(); expect(screen.getByText('Review queue')).toBeInTheDocument(); expect(screen.getByText('My submissions')).toBeInTheDocument() })
it('separates account access from installation licensing', () => { state.capabilities.education_enabled = false; render(<MemoryRouter><EducationWorkbench /></MemoryRouter>); expect(screen.getByText('Education access unavailable')).toBeInTheDocument(); expect(screen.getByText(/organization assignment and the active server license/)).toBeInTheDocument(); expect(screen.queryByText('Education entitlement required')).not.toBeInTheDocument() })
it('explains installation licensing when unavailable', () => { state.licensed = false; render(<MemoryRouter><EducationWorkbench /></MemoryRouter>); expect(screen.getByText('Education entitlement required')).toBeInTheDocument(); expect(screen.getByText(/not individual users/)).toBeInTheDocument() })
it('does not interpret failed requests as license failures', () => { state.error = true; render(<MemoryRouter><EducationWorkbench /></MemoryRouter>); expect(screen.getByText('Unable to load Education access')).toBeInTheDocument(); expect(screen.queryByText('Education entitlement required')).not.toBeInTheDocument() })
it('waits for license loading before showing an access failure', () => { state.loading = true; state.licensed = false; render(<MemoryRouter><EducationWorkbench /></MemoryRouter>); expect(screen.getByText('Loading Education workspace…')).toBeInTheDocument(); expect(screen.queryByText('Education entitlement required')).not.toBeInTheDocument() })

it('shows a license request failure as unavailable access rather than missing entitlement', () => { state.licenseError = true; state.licensed = false; render(<MemoryRouter><EducationWorkbench /></MemoryRouter>); expect(screen.getByText('Unable to load Education access')).toBeInTheDocument(); expect(screen.queryByText('Education entitlement required')).not.toBeInTheDocument() })

it('renders exactly one submission queue as the tabs change', () => {
  // Both queues once shared key={audience} as siblings, so switching tabs left a stale queue mounted.
  Object.assign(state.capabilities, { student: true, manager: true })
  render(<MemoryRouter><EducationWorkbench /></MemoryRouter>)
  for (const [tab, expected] of [['My submissions', 'mine'], ['Review queue', 'review'], ['My submissions', 'mine']] as const) {
    fireEvent.click(screen.getByRole('button', { name: tab }))
    expect(screen.getAllByTestId('submission-queue').map((node) => node.textContent)).toEqual([expected])
  }
})

it('allows platform school setup without granting classroom capabilities', () => {
  state.principal = { id: 88, username: 'platform', role: 'admin', group_id: null }
  state.capabilities.education_enabled = false
  render(<MemoryRouter><EducationWorkbench /></MemoryRouter>)
  expect(screen.getByText('Education administration')).toBeInTheDocument()
  expect(screen.getByLabelText('School to administer')).toHaveValue('7')
  expect(screen.queryByTestId('submission-queue')).not.toBeInTheDocument()
})

vi.mock('../../components/education/EducationQueues', () => ({ default: ({ kind, readOnly, orgId }: any) => <div data-testid="education-queues">{kind}:{String(readOnly)}:{orgId}</div> }))
it('students have only their submissions and no staff queue tabs', () => {
  state.capabilities.student = true
  render(<MemoryRouter><EducationWorkbench /></MemoryRouter>)
  expect(screen.getByText('My submissions')).toBeInTheDocument()
  for (const name of ['Review queue', 'Job Queue', 'Archive']) expect(screen.queryByRole('button', { name })).not.toBeInTheDocument()
})
it.each(['jobs', 'archive'])('opens the %s deep link and preserves other query parameters', (tab) => {
  window.history.replaceState({}, '', `/?tab=${tab}&keep=1`)
  state.capabilities.manager = true
  render(<MemoryRouter><EducationWorkbench /></MemoryRouter>)
  expect(screen.getByTestId('education-queues')).toHaveTextContent(`${tab}:`)
  fireEvent.click(screen.getByRole('button', { name: 'Review queue' }))
  expect(new URLSearchParams(window.location.search).get('tab')).toBe('review')
  expect(new URLSearchParams(window.location.search).get('keep')).toBe('1')
  window.history.replaceState({}, '', '/')
})
it('platform queues use explicit school scope and read only', () => {
  window.history.replaceState({}, '', '/?tab=jobs')
  state.principal = { id: 88, username: 'platform', role: 'admin', group_id: null }
  render(<MemoryRouter><EducationWorkbench /></MemoryRouter>)
  expect(screen.getByTestId('education-queues')).toHaveTextContent('jobs:true:7')
  window.history.replaceState({}, '', '/')
})
it('preserves Classroom callback behavior alongside tab links', () => {
  window.history.replaceState({}, '', '/?tab=jobs&classroom=connected&keep=1')
  state.capabilities.tenant_admin = true
  render(<MemoryRouter><EducationWorkbench /></MemoryRouter>)
  expect(new URLSearchParams(window.location.search).get('tab')).toBe('classroom')
  expect(new URLSearchParams(window.location.search).has('classroom')).toBe(false)
  expect(new URLSearchParams(window.location.search).get('keep')).toBe('1')
  window.history.replaceState({}, '', '/')
})
