import { fetchAPI } from './client'
import type {
  CursorPage,
  EducationCapabilities,
  EducationAMSReport,
  EducationApprovalRequest,
  EducationCompatibility,
  EducationCostCenter,
  EducationDecisionResult,
  EducationGrantPage,
  EducationPrinterPage,
  EducationSubmission,
  EducationSubmissionCreateResult,
  EducationSubmissionFilters,
  EducationReadiness,
} from '../types'

const queryString = (values: Record<string, string | number | boolean | undefined | null>) => {
  const query = new URLSearchParams()
  Object.entries(values).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') query.set(key, String(value))
  })
  const encoded = query.toString()
  return encoded ? `?${encoded}` : ''
}

export interface EducationQueueItem {
  id: number; job_id: number; item_name: string; submitter_username: string; center_name: string;
  cost_center_id: number; job_status: string; lifecycle_revision: number; archived?: boolean;
  section: 'printing' | 'committed' | 'waiting'; can_reorder: boolean; can_archive: boolean; blocked_reason?: string | null
}
export interface EducationPrinterQueue { printer_id: number; printer_name: string; revision: string; items: EducationQueueItem[] }
export interface EducationQueueResult { items?: EducationQueueItem[]; printers?: EducationPrinterQueue[] }

export const education = {
  queue: (kind: 'review' | 'jobs' | 'archive', options: { orgId?: number | null; archived?: string } = {}): Promise<EducationQueueResult> => fetchAPI(
    `/education/queues/${kind}${queryString({ org_id: options.orgId, archived: options.archived })}`),
  reorderQueue: (printerId: number, data: { revision: string; job_ids: number[] }): Promise<{ revision: string; job_ids: number[] }> => fetchAPI(
    `/education/queues/jobs/${printerId}/order`, { method: 'PATCH', body: JSON.stringify(data) }),
  capabilities: (): Promise<EducationCapabilities> => fetchAPI('/education/capabilities'),
  readiness: (orgId?: number | null): Promise<EducationReadiness> => fetchAPI(`/education/readiness${queryString({ org_id: orgId })}`),

  listCenters: (options: {
    includeArchived?: boolean
    cursor?: string
    limit?: number
    orgId?: number | null
  } = {}): Promise<CursorPage<EducationCostCenter>> => fetchAPI(
    `/education/cost-centers${queryString({
      include_archived: options.includeArchived,
      cursor: options.cursor,
      limit: options.limit,
      org_id: options.orgId,
    })}`
  ),
  getCenter: (id: number, orgId?: number | null): Promise<EducationCostCenter> => fetchAPI(
    `/education/cost-centers/${id}${queryString({ org_id: orgId })}`
  ),
  createCenter: (data: {
    org_id?: number | null
    name: string
    code: string
    description: string
    command_id: string
  }): Promise<EducationCostCenter> => fetchAPI('/education/cost-centers', {
    method: 'POST', body: JSON.stringify(data),
  }),
  updateCenter: (id: number, data: {
    org_id?: number | null
    revision: number
    name?: string
    code?: string
    description?: string
    command_id: string
  }): Promise<EducationCostCenter> => fetchAPI(`/education/cost-centers/${id}`, {
    method: 'PATCH', body: JSON.stringify(data),
  }),
  setCenterLifecycle: (
    id: number,
    action: 'archive' | 'reopen',
    data: { org_id?: number | null; revision: number; reason: string; command_id: string },
  ): Promise<EducationCostCenter> => fetchAPI(`/education/cost-centers/${id}/${action}`, {
    method: 'POST', body: JSON.stringify(data),
  }),

  listGrants: (centerId: number, cursor?: string, orgId?: number): Promise<EducationGrantPage> => fetchAPI(
    `/education/cost-centers/${centerId}/grants${queryString({ cursor, limit: 100, org_id: orgId })}`
  ),
  replaceGrants: (centerId: number, data: {
    org_id?: number
    revision: number
    grants: Array<{ user_id: number; roles: Array<'student' | 'manager'> }>
    command_id: string
  }): Promise<{ id: number; revision: number; grants: Array<{ user_id: number; roles: string[] }> }> => fetchAPI(
    `/education/cost-centers/${centerId}/grants`, { method: 'PUT', body: JSON.stringify(data) }
  ),
  listCenterPrinters: (centerId: number, cursor?: string, orgId?: number): Promise<EducationPrinterPage> => fetchAPI(
    `/education/cost-centers/${centerId}/printers${queryString({ cursor, limit: 100, org_id: orgId })}`
  ),
  replaceCenterPrinters: (centerId: number, data: {
    org_id?: number
    revision: number
    printer_ids: number[]
    command_id: string
  }): Promise<{ id: number; revision: number; printer_ids: number[] }> => fetchAPI(
    `/education/cost-centers/${centerId}/printers`, { method: 'PUT', body: JSON.stringify(data) }
  ),

  listSubmissions: (filters: EducationSubmissionFilters = {}): Promise<CursorPage<EducationSubmission>> => fetchAPI(
    `/education/submissions${queryString({
      status: filters.status,
      archived: filters.archived,
      cost_center_id: filters.costCenterId,
      cursor: filters.cursor,
      limit: filters.limit,
    })}`
  ),
  previewCompatibility: (
    id: number,
    printerId: number,
    revision: number,
  ): Promise<EducationCompatibility> => fetchAPI(
    `/education/submissions/${id}/compatibility${queryString({
      printer_id: printerId,
      revision,
    })}`
  ),
  uploadSubmission: (
    costCenterId: number,
    file: File,
    submissionToken = crypto.randomUUID(),
  ): Promise<EducationSubmissionCreateResult> => {
    const data = new FormData()
    data.append('submission_token', submissionToken)
    data.append('cost_center_id', String(costCenterId))
    data.append('file', file)
    return fetchAPI('/education/submissions', { method: 'POST', body: data })
  },
  readAMS: (
    id: number,
    data: { revision: number; printer_id: number; selected_slots: number[] },
  ): Promise<EducationAMSReport> => fetchAPI(`/education/submissions/${id}/ams-read`, {
    method: 'POST', body: JSON.stringify(data),
  }),
  approveSubmission: (
    id: number,
    data: EducationApprovalRequest,
  ): Promise<EducationDecisionResult> => fetchAPI(`/education/submissions/${id}/approve`, {
    method: 'POST', body: JSON.stringify(data),
  }),
  archiveSubmission: (id: number, data: { revision: number; reason?: string; command_id: string }) => fetchAPI(`/education/submissions/${id}/archive`, { method: 'POST', body: JSON.stringify(data) }),
  rejectSubmission: (
    id: number,
    data: { revision: number; reason: string; command_id: string },
  ): Promise<EducationDecisionResult> => fetchAPI(`/education/submissions/${id}/reject`, {
    method: 'POST', body: JSON.stringify(data),
  }),
}
