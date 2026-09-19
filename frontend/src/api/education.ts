import { fetchAPI } from './client'
import type {
  CursorPage,
  EducationCapabilities,
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

export const education = {
  capabilities: (): Promise<EducationCapabilities> => fetchAPI('/education/capabilities'),
  readiness: (): Promise<EducationReadiness> => fetchAPI('/education/readiness'),

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

  listGrants: (centerId: number, cursor?: string): Promise<EducationGrantPage> => fetchAPI(
    `/education/cost-centers/${centerId}/grants${queryString({ cursor, limit: 100 })}`
  ),
  replaceGrants: (centerId: number, data: {
    revision: number
    grants: Array<{ user_id: number; roles: Array<'student' | 'manager'> }>
    command_id: string
  }): Promise<{ id: number; revision: number; grants: Array<{ user_id: number; roles: string[] }> }> => fetchAPI(
    `/education/cost-centers/${centerId}/grants`, { method: 'PUT', body: JSON.stringify(data) }
  ),
  listCenterPrinters: (centerId: number, cursor?: string): Promise<EducationPrinterPage> => fetchAPI(
    `/education/cost-centers/${centerId}/printers${queryString({ cursor, limit: 100 })}`
  ),
  replaceCenterPrinters: (centerId: number, data: {
    revision: number
    printer_ids: number[]
    command_id: string
  }): Promise<{ id: number; revision: number; printer_ids: number[] }> => fetchAPI(
    `/education/cost-centers/${centerId}/printers`, { method: 'PUT', body: JSON.stringify(data) }
  ),

  listSubmissions: (filters: EducationSubmissionFilters = {}): Promise<CursorPage<EducationSubmission>> => fetchAPI(
    `/education/submissions${queryString({
      status: filters.status,
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
  approveSubmission: (
    id: number,
    data: { revision: number; printer_id: number; command_id: string },
  ): Promise<EducationDecisionResult> => fetchAPI(`/education/submissions/${id}/approve`, {
    method: 'POST', body: JSON.stringify(data),
  }),
  rejectSubmission: (
    id: number,
    data: { revision: number; reason: string; command_id: string },
  ): Promise<EducationDecisionResult> => fetchAPI(`/education/submissions/${id}/reject`, {
    method: 'POST', body: JSON.stringify(data),
  }),
}
