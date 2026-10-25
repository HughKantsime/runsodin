// Archive domain types

import type { PaginatedResponse } from './common';

export interface Archive {
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

export interface ArchiveAmsRequirement { slot: number; color: string; material: string }
export interface ArchiveAmsSlot { slot: number; color: string; color_hex: string; material: string; spool_id: number | null }
export interface ArchiveAmsSuggestion { source_slot: number; target_slot: number; confidence: 'exact' | 'material_only' }

export interface ArchiveAmsPreview {
  required: ArchiveAmsRequirement[];
  available: ArchiveAmsSlot[];
  suggested_mapping: ArchiveAmsSuggestion[];
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
