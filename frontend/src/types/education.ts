export type EducationSubmissionStatus =
  | 'submitted'
  | 'pending'
  | 'scheduled'
  | 'printing'
  | 'completed'
  | 'failed'
  | 'rejected'
  | 'cancelled'

export interface EducationCapabilities {
  education_enabled: boolean
  student: boolean
  manager: boolean
  tenant_admin: boolean
  student_cost_center_ids: number[]
  managed_cost_center_ids: number[]
}

export interface EducationCostCenter {
  id: number
  org_id: number
  name: string
  code: string
  description: string
  active: boolean
  revision: number
  created_at: string
  updated_at: string
  counts: {
    active_grants: number
    active_printers: number
    submissions: number
  }
}

export interface EducationGrant {
  grant_id: number
  user_id: number
  username: string
  display_name: string | null
  role: 'student' | 'manager'
  state: 'active' | 'revoked'
  granted_at: string | null
  revoked_at: string | null
}

export interface EducationPrinterEntitlement {
  entitlement_id: number
  printer_id: number
  name: string
  machine_type: string | null
  api_type: string | null
  state: 'active' | 'revoked'
  granted_at: string | null
  revoked_at: string | null
}

export interface EducationSubmission {
  id: number
  job_id: number
  cost_center_id: number
  submitted_by: number
  submitter_username: string
  item_name: string
  status: EducationSubmissionStatus
  approved_printer_id: number | null
  lifecycle_revision: number
  compatibility_engine_version: string | null
  rejection_reason: string | null
  created_at: string | null
  updated_at: string | null
}

export interface CursorPage<T> {
  items: T[]
  next_cursor: string | null
}

export interface EducationGrantPage extends CursorPage<EducationGrant> {
  center_revision: number
}

export interface EducationPrinterPage extends CursorPage<EducationPrinterEntitlement> {
  center_revision: number
}

export interface EducationSubmissionFilters {
  status?: EducationSubmissionStatus
  costCenterId?: number
  cursor?: string
  limit?: number
}

export interface EducationSubmissionCreateResult {
  id: number
  job_id: number
  print_file_id: number
  model_id: number
  cost_center_id: number
  status: EducationSubmissionStatus
  lifecycle_revision: number
}

export interface EducationDecisionResult {
  id: number
  job_id: number
  cost_center_id: number
  status: EducationSubmissionStatus
  approved_printer_id: number | null
  lifecycle_revision: number
  compatibility_engine_version: string | null
  compatibility?: {
    compatible: boolean
    reasons: string[]
    engine_version: string
  }
}

export interface EducationCompatibility {
  submission_id: number
  printer_id: number
  lifecycle_revision: number
  compatible: boolean
  reasons: string[]
  engine_version: string
}

export interface ClassroomStatus {
  configured: boolean
  state: 'not_connected' | 'connected' | 'reconnect_required'
  connected: boolean
  client_id: string
  account_email: string | null
  allowed_domains: string
  last_success_at: string | null
  last_error_code: string | null
}

export interface ClassroomCourse {
  id: string
  name: string
  section: string
  description: string
  course_state: string
}

export interface ClassroomMember {
  provider_user_id: string
  email: string
  name: string
}

export interface ClassroomRosterPreview {
  course: ClassroomCourse
  teachers: ClassroomMember[]
  students: ClassroomMember[]
  mapping: { cost_center_id: number; last_imported_at: string | null } | null
  diff: {
    added_or_changed: string[]
    removed: string[]
    unchanged: number
  }
}

export interface ClassroomImportResult {
  course_id: string
  cost_center_id: number
  created_cost_center: boolean
  teachers: number
  students: number
  revision: number
}

export interface EducationReadiness {
  education_license: boolean
  education_mode: boolean
  oidc: { ready: boolean; provider: string; enabled: boolean }
  classroom: ClassroomStatus
  pilot: {
    active_centers: number
    student_grants: number
    manager_grants: number
    printer_entitlements: number
  }
  backup: { database_backend: string; verified_workflow_available: boolean }
}
