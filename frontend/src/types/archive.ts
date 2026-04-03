// Archive domain types

import type { PaginatedResponse } from './common';

export interface Archive {
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
  a: Archive;
  b: Archive;
  [key: string]: unknown;
}

export interface ArchiveAmsPreview {
  [key: string]: unknown;
}

export interface ArchiveReprintRequest {
  printer_id?: number;
  [key: string]: unknown;
}

// ---- Projects ----

export interface Project {
  id: number;
  name: string;
  description: string | null;
  status: string;
  created_at: string;
  updated_at: string;
  archives: Archive[];
  [key: string]: unknown;
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

export interface Timelapse {
  id: number;
  printer_id: number;
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
