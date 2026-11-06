import { fetchAPI } from './client'

export interface ClassRow {
  id: string; cost_center_id: number; title: string; term: string; state: 'active' | 'archived';
  revision: number; can_manage: boolean; can_submit: boolean
}
export interface AssignmentBody {
  title: string; brief: string; due_at: string | null; max_print_seconds: number | null;
  max_material_grams: number | null; allowed_materials: string[]; submission_quota: number | null;
  auto_print_seconds: number | null; auto_material_grams: number | null; auto_printer_ids: number[];
  state: 'active' | 'closed'
}
export interface AssignmentRow extends AssignmentBody { id: string; class_id: string; revision: number }
export interface PortfolioItem {
  submission_id: number; job_id: number; cost_center_id: number; item_name: string; student_username: string;
  status: string; lifecycle_revision: number; assignment_id: string | null; assignment_title: string | null;
  class_title: string | null; term: string | null; feedback: string | null; grade: string | null;
  learning_status: string | null; assessment_revision: number | null; thumbnail_url: string; timelapse_ids: number[]
}
export interface Page<T> { items: T[]; next_offset: number | null }
const base = '/education/classes'
const body = (data: unknown, method: string) => ({ method, body: JSON.stringify(data) })
export const educationClasses = {
  list: (offset = 0): Promise<Page<ClassRow>> => fetchAPI(`${base}?offset=${offset}`),
  create: (data: { cost_center_id: number; title: string; term: string }): Promise<ClassRow> => fetchAPI(base, body(data, 'POST')),
  update: (c: ClassRow, data: { title: string; term: string; state: string }): Promise<ClassRow> => fetchAPI(`${base}/${c.id}`, body({ ...data, revision: c.revision }, 'PUT')),
  roster: (id: string): Promise<{ items: Array<{ user_id: number; username: string; role: 'manager' | 'student' }> }> => fetchAPI(`${base}/${id}/roster`),
  assignments: (id: string, offset = 0): Promise<Page<AssignmentRow>> => fetchAPI(`${base}/${id}/assignments?offset=${offset}`),
  save: (classId: string, data: AssignmentBody, existing?: AssignmentRow): Promise<AssignmentRow> => fetchAPI(
    `${base}/${classId}/assignments${existing ? `/${existing.id}` : ''}`,
    body(existing ? { ...data, revision: existing.revision } : data, existing ? 'PUT' : 'POST')),
  portfolio: (assignmentId?: string, offset = 0): Promise<Page<PortfolioItem>> => fetchAPI(
    assignmentId ? `${base}/assignments/${assignmentId}/submissions?offset=${offset}` : `${base}/portfolio?offset=${offset}`),
  link: (assignmentId: string, submissionId: number, revision: number): Promise<{ linked: boolean; auto_approval: string }> => fetchAPI(
    `${base}/assignments/${assignmentId}/submissions`, body({ submission_id: submissionId, revision }, 'POST')),
  assess: (assignmentId: string, item: PortfolioItem, data: { feedback: string; grade: string; learning_status: string }): Promise<unknown> => fetchAPI(
    `${base}/assignments/${assignmentId}/submissions/${item.submission_id}`, body({ ...data, revision: item.assessment_revision }, 'PUT')),
}
