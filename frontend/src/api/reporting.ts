import { fetchAPI } from './client'

// ---- Reporting types ----

export interface AnalyticsData {
  jobs_by_date: Record<string, { created: number; completed: number }>;
  top_by_jobs: { id: number; name: string; job_count: number }[];
  [key: string]: unknown;
}

/** GET /analytics summary; money fields are null when a counted job has no price/cost. */
export interface AnalyticsSummary {
  total_models: number;
  total_jobs: number;
  completed_jobs: number;
  pending_jobs: number;
  total_revenue: number | null;
  total_cost: number | null;
  total_margin: number | null;
  margin_percent: number | null;
  projected_revenue: number | null;
  projected_cost: number | null;
  total_print_hours: number;
  avg_value_per_hour: number;
  jobs_with_cost_data: number;
  jobs_with_unknown_quantity: number;
  jobs_unpriced?: number;
  jobs_uncosted?: number;
  pending_unpriced?: number;
  pending_uncosted?: number;
  pending_scope?: 'current_queue';
}

export interface TimeAccuracyData {
  [key: string]: unknown;
}

export interface FailureAnalyticsData {
  total_failed: number;
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

export interface EducationMetrics {
  total_jobs_submitted: number;
  total_jobs_approved: number;
  total_jobs_rejected: number;
  total_jobs_completed: number;
  total_jobs_failed: number;
  total_print_hours: number | null;
  estimated_print_hours: number | null;
  estimated_filament_grams: number | null;
  unknown_actual_hours_jobs: number;
  unknown_estimated_hours_jobs: number;
  unknown_filament_estimate_jobs: number;
  accuracy_samples: number;
  unknown_accuracy_jobs: number;
  unproven_history_jobs: number;
  total_cost: number | null;
  unknown_cost_jobs: number;
  approval_rate: number;
  success_rate: number;
  accuracy: number | null;
  currency: string | null;
}
export interface EducationUserMetrics extends EducationMetrics {
  user_id: number;
  username: string;
  email: string | null;
  role: string;
  last_activity: string | null;
}
export interface EducationUsageReport {
  summary: EducationMetrics & { total_users_active: number; total_jobs: number; rejection_rate: number };
  users: EducationUserMetrics[];
  daily_submissions: Record<string, number>;
  by_printer: (EducationMetrics & { id: number; name: string | null; available: boolean })[];
  by_model: (EducationMetrics & { id: number; name: string | null; available: boolean })[];
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
  get: (days?: number): Promise<AnalyticsData> => fetchAPI('/analytics' + (days ? `?days=${days}` : '')),
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
