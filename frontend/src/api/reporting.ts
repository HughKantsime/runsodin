import { fetchAPI } from './client'

// ---- Reporting types ----

export interface AnalyticsData {
  [key: string]: unknown;
}

export interface TimeAccuracyData {
  [key: string]: unknown;
}

export interface FailureAnalyticsData {
  [key: string]: unknown;
}

export interface PrinterStats {
  id: number;
  name: string;
  completed_jobs: number;
  failed_jobs: number;
  total_hours: number;
  utilization_pct: number;
  success_rate: number;
  avg_job_hours: number;
}

export interface StatsData {
  printers: { total: number; active: number };
  jobs: { pending: number; scheduled: number; printing: number; completed_today: number };
  models: number;
  spoolman_connected: boolean;
  printer_stats: PrinterStats[];
}

export interface EducationUsageReport {
  [key: string]: unknown;
}

export interface ReportSchedule {
  id: number;
  name: string;
  report_type: string;
  schedule: string;
  recipients: string[];
  enabled: boolean;
  last_run_at: string | null;
  created_at: string;
  updated_at: string;
  [key: string]: unknown;
}

export interface ReportScheduleCreate {
  name: string;
  report_type: string;
  schedule: string;
  recipients?: string[];
  enabled?: boolean;
  [key: string]: unknown;
}

export interface ReportScheduleUpdate {
  name?: string;
  report_type?: string;
  schedule?: string;
  recipients?: string[];
  enabled?: boolean;
  [key: string]: unknown;
}

export const analytics = {
  get: (): Promise<AnalyticsData> => fetchAPI('/analytics'),
  timeAccuracy: (days = 30): Promise<TimeAccuracyData> => fetchAPI(`/analytics/time-accuracy?days=${days}`),
  failures: (days = 30): Promise<FailureAnalyticsData> => fetchAPI(`/analytics/failures?days=${days}`),
}

export const stats = {
  get: (days?: number): Promise<StatsData> => fetchAPI('/stats' + (days ? `?days=${days}` : '')),
}

export const educationReports = {
  getUsageReport: (days = 30): Promise<EducationUsageReport> => fetchAPI(`/education/usage-report?days=${days}`),
}

// Scheduled Reports
export const reportSchedules = {
  list: (): Promise<ReportSchedule[]> => fetchAPI('/report-schedules'),
  create: (data: ReportScheduleCreate): Promise<ReportSchedule> => fetchAPI('/report-schedules', { method: 'POST', body: JSON.stringify(data) }),
  update: (id: number, data: ReportScheduleUpdate): Promise<ReportSchedule> => fetchAPI(`/report-schedules/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id: number): Promise<void> => fetchAPI(`/report-schedules/${id}`, { method: 'DELETE' }),
  runNow: (id: number): Promise<unknown> => fetchAPI(`/report-schedules/${id}/run`, { method: 'POST' }),
}
