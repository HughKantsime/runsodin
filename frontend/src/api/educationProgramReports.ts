import { fetchAPI, downloadBlob } from './client'

export interface ProgramFilters {
  start_date: string; end_date: string; class_id?: string; cost_center_id?: number; term?: string; template: 'generic' | 'perkins';
}
export interface ProgramMark { user_id: number; participant: boolean; concentrator: boolean; revision: number }
export interface InstructorHours { id?: string; instructor_id: number; activity_date: string; hours: number; revision: number }
export interface ProgramReport {
  metric_labels?: Record<string, string>; timezone?: string;
  notice: string; currency: string | null; summary: Record<string, number | null>;
  basis: Record<string, string>; classes: Array<Record<string, string | number | null>>;
}
const base = '/education/program-reports'
const query = (filters: ProgramFilters) => new URLSearchParams(Object.entries(filters).filter(([, v]) => v !== undefined).map(([k, v]) => [k, String(v)])).toString()
export const educationProgramReports = {
  options: (): Promise<{ timezone: string; perkins_enabled: boolean }> => fetchAPI(`${base}/options`),
  get: (filters: ProgramFilters): Promise<ProgramReport> => fetchAPI(`${base}?${query(filters)}`),
  download: (filters: ProgramFilters, format: 'csv' | 'pdf') => downloadBlob(`${base}?${query(filters)}&format=${format}`, `odin-program-report.${format}`),
  records: (classId: string, day?: string): Promise<{ marks: ProgramMark[]; hours: InstructorHours[]; can_approve_hours: boolean }> => fetchAPI(`${base}/classes/${classId}${day ? `?activity_date=${encodeURIComponent(day)}` : ''}`),
  mark: (classId: string, row: ProgramMark) => fetchAPI(`${base}/classes/${classId}/marks/${row.user_id}`, { method: 'PUT', body: JSON.stringify({ participant: row.participant, concentrator: row.concentrator, revision: row.revision }) }),
  hours: (classId: string, row: InstructorHours) => fetchAPI(`${base}/classes/${classId}/instructor-hours`, { method: 'PUT', body: JSON.stringify({ instructor_id: row.instructor_id, activity_date: row.activity_date, hours: row.hours, revision: row.revision }) }),
}
