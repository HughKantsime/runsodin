// Job domain types

import type { FilamentType, PrinterSummary } from './printer';

export type JobStatus =
  | 'pending'
  | 'scheduled'
  | 'printing'
  | 'paused'
  | 'completed'
  | 'failed'
  | 'cancelled';

export interface ModelSummary {
  id: number;
  name: string;
}

export interface Job {
  id: number;
  item_name: string;
  model_id: number | null;
  quantity: number;
  priority: number;
  duration_hours: number | null;
  colors_required: string | null;
  filament_type: FilamentType | null;
  notes: string | null;
  hold: boolean;
  due_date: string | null;
  required_tags: string[];
  target_type: string | null;
  target_filter: string | null;
  status: JobStatus;
  printer_id: number | null;
  scheduled_start: string | null;
  scheduled_end: string | null;
  actual_start: string | null;
  actual_end: string | null;
  match_score: number | null;
  is_locked: boolean;
  created_at: string;
  updated_at: string;
  colors_list: string[];
  effective_duration: number;
  estimated_cost: number | null;
  suggested_price: number | null;
  model_revision_id: number | null;
  order_item_id: number | null;
  quantity_on_bed: number | null;
  printer: PrinterSummary | null;
  model: ModelSummary | null;
}

export interface JobCreate {
  item_name: string;
  model_id?: number;
  quantity?: number;
  priority?: number | string;
  duration_hours?: number;
  colors_required?: string;
  filament_type?: FilamentType;
  notes?: string;
  hold?: boolean;
  due_date?: string;
  required_tags?: string[];
  target_type?: string;
  target_filter?: string;
  model_revision_id?: number;
}

export interface JobUpdate {
  item_name?: string;
  model_id?: number;
  quantity?: number;
  priority?: number;
  status?: JobStatus;
  printer_id?: number;
  scheduled_start?: string;
  scheduled_end?: string;
  duration_hours?: number;
  colors_required?: string;
  filament_type?: FilamentType;
  notes?: string;
  hold?: boolean;
  is_locked?: boolean;
  due_date?: string;
}

export interface JobSummary {
  id: number;
  item_name: string;
  status: JobStatus;
  priority: number;
  printer_id: number | null;
  printer_name: string | null;
  scheduled_start: string | null;
  scheduled_end: string | null;
  duration_hours: number | null;
  colors_list: string[];
  match_score: number | null;
}

// ---- Scheduler ----

export interface ScheduleResult {
  success: boolean;
  run_id: number;
  scheduled: number;
  skipped: number;
  setup_blocks: number;
  message: string;
  jobs: JobSummary[];
}

// ---- Timeline ----

export interface TimelineSlot {
  start: string;
  end: string;
  printer_id: number;
  printer_name: string;
  job_id: number | null;
  item_name: string | null;
  status: JobStatus | null;
  mqtt_job_id: number | null;
  is_setup: boolean;
  colors: string[];
}

export interface TimelineResponse {
  start_date: string;
  end_date: string;
  slot_duration_minutes: number;
  printers: PrinterSummary[];
  slots: TimelineSlot[];
}

// ---- Print Jobs (MQTT tracked) ----

export interface PrintJob {
  id: number;
  printer_id: number;
  job_id: number | null;
  file_name: string | null;
  status: string;
  progress: number;
  started_at: string | null;
  completed_at: string | null;
  duration_seconds: number | null;
  filament_used_g: number | null;
  [key: string]: unknown;
}

export interface PrintJobStats {
  total: number;
  completed: number;
  failed: number;
  avg_duration_hours: number | null;
  [key: string]: unknown;
}

// ---- Print Files ----

export interface PrintFile {
  id: number;
  filename: string;
  file_size: number | null;
  uploaded_at: string;
  metadata: Record<string, unknown> | null;
  [key: string]: unknown;
}

// ---- Presets ----

export interface Preset {
  id: number;
  name: string;
  [key: string]: unknown;
}

export interface PresetCreate {
  [key: string]: unknown;
}

// ---- Job Move ----

export interface JobMoveRequest {
  printer_id: number;
  scheduled_start?: string;
}

// ---- Failure Logging ----

export interface FailureReason {
  id: number;
  name: string;
  [key: string]: unknown;
}

export interface JobFailureUpdate {
  fail_reason: string;
  fail_notes?: string;
}

// ---- Approval ----

export interface ApprovalSetting {
  enabled: boolean;
}
