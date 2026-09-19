import { fetchAPI } from './client'
import type {
  ClassroomCourse,
  ClassroomImportResult,
  ClassroomRosterPreview,
  ClassroomStatus,
} from '../types'

export const classroom = {
  status: (): Promise<ClassroomStatus> => fetchAPI('/education/classroom/status'),
  configure: (data: {
    client_id: string
    client_secret?: string
    allowed_domains: string
  }): Promise<ClassroomStatus> => fetchAPI('/education/classroom/config', {
    method: 'PUT', body: JSON.stringify(data),
  }),
  connectUrl: (): Promise<{ authorization_url: string; redirect_uri: string }> => fetchAPI(
    '/education/classroom/connect-url', { method: 'POST' }
  ),
  disconnect: (): Promise<ClassroomStatus> => fetchAPI('/education/classroom/disconnect', { method: 'POST' }),
  courses: (): Promise<{ items: ClassroomCourse[] }> => fetchAPI('/education/classroom/courses'),
  preview: (courseId: string): Promise<ClassroomRosterPreview> => fetchAPI(
    `/education/classroom/courses/${encodeURIComponent(courseId)}/preview`
  ),
  importCourse: (courseId: string, data: {
    cost_center_id?: number
    update_metadata: boolean
    command_id: string
  }): Promise<ClassroomImportResult> => fetchAPI(
    `/education/classroom/courses/${encodeURIComponent(courseId)}/import`,
    { method: 'POST', body: JSON.stringify(data) },
  ),
}
