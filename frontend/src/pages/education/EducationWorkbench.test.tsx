import React from 'react'
import { MemoryRouter } from 'react-router-dom'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import EducationWorkbench from './EducationWorkbench'
const state = vi.hoisted(() => ({
  capabilities: { education_enabled: true, student: false, manager: false, tenant_admin: false, student_cost_center_ids: [], managed_cost_center_ids: [] },
  licensed: true, error: false, loading: false, licenseError: false,
}))
vi.mock('../../api', () => ({ education: {}, getEducationMode: vi.fn() }))
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ isEducation: state.licensed, hasFeature: () => state.licensed, loading: state.loading, loadError: state.licenseError }) }))
vi.mock('@tanstack/react-query', () => ({
  useQueryClient: () => ({}),
  useQuery: ({ queryKey }: { queryKey: string[] }) => ({ data: queryKey[0] === 'education-mode' ? { enabled: true } : state.capabilities, isError: state.error, isLoading: false }),
  useInfiniteQuery: () => ({ data: { pages: [{ items: [] }] }, hasNextPage: false }),
}))
vi.mock('../../components/education/CostCenterManager', () => ({ default: () => null }))
vi.mock('../../components/education/ClassroomManager', () => ({ default: () => null }))
vi.mock('../../components/education/SubmissionQueue', () => ({ default: () => null }))
vi.mock('../../components/education/SubmissionUploadModal', () => ({ default: () => null }))
vi.mock('../../components/education/ReadinessPanel', () => ({ default: () => null }))
beforeEach(() => { Object.assign(state.capabilities, { education_enabled: true, student: false, manager: false, tenant_admin: false }); state.licensed = true; state.error = false; state.loading = false; state.licenseError = false })
afterEach(cleanup)
it('does not call an account without grants a student', () => { render(<MemoryRouter><EducationWorkbench /></MemoryRouter>); expect(screen.getByText('No classroom access')).toBeInTheDocument(); expect(screen.queryByText('Student')).not.toBeInTheDocument(); expect(screen.getByText(/An Operator needs Manager/)).toBeInTheDocument() })
it('shows dual submission and review access', () => { Object.assign(state.capabilities, { student: true, manager: true }); render(<MemoryRouter><EducationWorkbench /></MemoryRouter>); expect(screen.getByText('Manager + Student')).toBeInTheDocument(); expect(screen.getByText('Review queue')).toBeInTheDocument(); expect(screen.getByText('My submissions')).toBeInTheDocument() })
it('separates account access from installation licensing', () => { state.capabilities.education_enabled = false; render(<MemoryRouter><EducationWorkbench /></MemoryRouter>); expect(screen.getByText('Education access unavailable')).toBeInTheDocument(); expect(screen.getByText(/organization assignment and the active server license/)).toBeInTheDocument(); expect(screen.queryByText('Education entitlement required')).not.toBeInTheDocument() })
it('explains installation licensing when unavailable', () => { state.licensed = false; render(<MemoryRouter><EducationWorkbench /></MemoryRouter>); expect(screen.getByText('Education entitlement required')).toBeInTheDocument(); expect(screen.getByText(/not individual users/)).toBeInTheDocument() })
it('does not interpret failed requests as license failures', () => { state.error = true; render(<MemoryRouter><EducationWorkbench /></MemoryRouter>); expect(screen.getByText('Unable to load Education access')).toBeInTheDocument(); expect(screen.queryByText('Education entitlement required')).not.toBeInTheDocument() })
it('waits for license loading before showing an access failure', () => { state.loading = true; state.licensed = false; render(<MemoryRouter><EducationWorkbench /></MemoryRouter>); expect(screen.getByText('Loading Education workspace…')).toBeInTheDocument(); expect(screen.queryByText('Education entitlement required')).not.toBeInTheDocument() })

it('shows a license request failure as unavailable access rather than missing entitlement', () => { state.licenseError = true; state.licensed = false; render(<MemoryRouter><EducationWorkbench /></MemoryRouter>); expect(screen.getByText('Unable to load Education access')).toBeInTheDocument(); expect(screen.queryByText('Education entitlement required')).not.toBeInTheDocument() })
