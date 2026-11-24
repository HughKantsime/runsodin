// Archive domain types

import type { PaginatedResponse } from './common';

export interface Archive {
  materials?: { material: string; grams: number | null }[];
  material?: string | null;
  material_source?: 'spool' | 'file' | 'job' | null;
  accounting_state?: string;
  settleable?: boolean;
  accounting_badge?: string | null;
  accounting_cause?: AccountingCause;
  accounting_settled?: { by: string | null; at: string } | null;
  consumption?: { spool_id: number | null; grams_used: number | null; slot_number?: number; reason?: string; remaining_unknown?: boolean; deducted_now_g?: number }[];
  print_job_id?: number;
  failure_correction_allowed?: boolean;
  failure_correction?: import('./job').FailureCorrection | null;
  display_name?: string | null;
  print_name: string;
  printer_display: string;
  printer_nickname: string | null;
  user_name: string | null;
  actual_duration_seconds: number | null;
  filament_used_grams: number | null;
  cost_estimate: number | null;
  started_at: string | null;
  id: number;
  item_name: string;
  printer_id: number | null;
  printer_name: string | null;
  status: string;
  duration_hours: number | null;
  filament_used_g: number | null;
  colors_used: string[];
  notes: string | null;
  tags: string[];
  completed_at: string | null;
  created_at: string;
  file_id: number | null;
  thumbnail_url: string | null;
  [key: string]: unknown;
}

export interface ArchiveUpdate {
  notes?: string;
  tags?: string[];
}

export interface ArchiveListParams {
  accounting?: string;
  page?: number;
  per_page?: number;
  printer_id?: number | string;
  status?: string;
  search?: string;
  start_date?: string;
  end_date?: string;
  tag?: string;
}

export interface ArchiveLogParams {
  page?: number;
  per_page?: number;
  printer_id?: number | string;
  user_id?: number | string;
  status?: string;
  search?: string;
  start_date?: string;
  end_date?: string;
  tag?: string;
}

export interface ArchiveCompareResult {
  diff: Record<string, { a: string | number | null; b: string | number | null }>;
  a: Archive;
  b: Archive;
  [key: string]: unknown;
}

// One-based file filament IDs (filaments_json.slot) and one-based printer trays.
export type AmsMapping = { source_slot: number; target_slot: number }[];

export interface ArchiveAmsRequirement { slot: number; color: string; material: string }
export interface ArchiveAmsSlot { catalogue_name?: string | null; do_not_feed?: boolean; slot: number; color: string; color_hex: string; material: string; spool_id: number | null }
export interface ArchiveAmsSuggestion { source_slot: number; target_slot: number; confidence: 'exact' | 'material_only' }

export interface ArchiveAmsPreview {
  planned_loads?: string[];
  required: ArchiveAmsRequirement[];
  available: ArchiveAmsSlot[];
  suggested_mapping: ArchiveAmsSuggestion[];
  /** Why this printer can't map the file's filaments; a job can still be created without a mapping. */
  file_note?: string;
  /** A material-only suggestion stays off until someone picks or confirms the slots. */
  confirm_suggestions?: boolean;
}

export interface ArchiveReprintRequest {
  printer_id?: number;
  plate_index?: number;
  ams_mapping?: { source_slot: number; target_slot: number }[];
  [key: string]: unknown;
}

// ---- Projects ----

export interface Project {
  color: string;
  archive_count: number;
  expected_parts: number | null;
  id: number;
  name: string;
  description: string | null;
  status: string;
  created_at: string;
  updated_at: string;
  archives: Archive[];
  [key: string]: unknown;
}

export interface ProjectImportResult {
  id: number
  name: string
  status: 'imported'
  archives: number
  files: number
}

export interface ProjectCreate {
  name: string;
  description?: string;
  status?: string;
}

export interface ProjectUpdate {
  name?: string;
  description?: string;
  status?: string;
}

// ---- Tags ----

export interface Tag {
  name: string;
  count: number;
}

export interface TagRenameRequest {
  old: string;
  new: string;
}

// ---- Timelapse ----

export interface TimelapseList {
  total: number; offset: number; limit: number; timelapses: Timelapse[];
}

export interface Timelapse {
  id: number;
  printer_id: number;
  printer_name?: string;
  print_job_id: number | null;
  print_name: string | null;
  filename: string;
  frame_count: number;
  duration_seconds: number | null;
  file_size_mb: number | null;
  status: string;
  created_at: string;
  completed_at: string | null;
}

export interface TimelapseListParams {
  printer_id?: number | string;
  status?: string;
  limit?: number;
  offset?: number;
}

export interface TimelapseTrimRequest {
  start_seconds: number;
  end_seconds: number;
}

export interface TimelapseSpeedRequest {
  multiplier: number;
}

export interface AccountingCause { code: string; title: string; next_step: string }
export interface SettlementPreview {
  archive_id: number; job_id: number | null; accounting_state: string; accounting_reason: string | null;
  cause: AccountingCause; settleable: boolean; not_settleable: string | null;
  grams: { measured: number | null; sliced_estimate: number | null };
  suggested: { slot_number: number | null; spool_id: number; spool_label: string; grams: number | null; remaining_g: number | null }[];
  manual_usage: { usage_id: number; spool_id: number; grams: number; used_at: string; notes: string }[];
  prior_usage: string | null;
  balance_set_after_print: { spool_id: number; at: string | null; how: string }[];
}
export type NoDeduction = 'nothing_extruded' | 'recorded_by_hand' | 'spool_not_tracked' | 'accept_existing' | 'balance_already_updated';
export interface FilamentSettlement {
  entries: { spool_id: number; grams: number }[];
  counted_usage_ids: number[]; separate_usage_ids: number[];
  no_deduction: NoDeduction | null; note: string; deduct_despite_later_balance: number[];
}
export interface AccountingSummary { unresolved: number; by_cause: { code: string; title: string; count: number }[]; oldest_completed_at: string | null }
