// Printer domain types

export type FilamentType =
  | 'empty'
  | 'Unknown'
  | 'PLA'
  | 'PETG'
  | 'ABS'
  | 'ASA'
  | 'TPU'
  | 'PA'
  | 'PC'
  | 'PVA'
  | 'OTHER'
  | 'PLA_SUPPORT'
  | 'PLA_CF'
  | 'PETG_CF'
  | 'NYLON_CF'
  | 'NYLON_GF'
  | 'PC_ABS'
  | 'PC_CF'
  | 'SUPPORT'
  | 'HIPS'
  | 'PPS'
  | 'PPS_CF';

// ---- Filament Slots ----

export interface FilamentSlot {
  id: number;
  printer_id: number;
  slot_number: number;
  filament_type: FilamentType;
  color: string | null;
  color_hex: string | null;
  spoolman_spool_id: number | null;
  assigned_spool_id: number | null;
  spool_confirmed: boolean | null;
  loaded_at: string | null;
  remaining: number | null;
  material_type: string | null;
}

export interface FilamentSlotUpdate {
  filament_type?: FilamentType;
  color?: string;
  color_hex?: string;
  spoolman_spool_id?: number | null;
  assigned_spool_id?: number | null;
  spool_confirmed?: boolean | null;
}

// ---- Printer ----

export interface Printer {
  id: number;
  name: string;
  model: string | null;
  slot_count: number;
  is_active: boolean;
  api_type: string | null;
  api_host: string | null;
  camera_url: string | null;
  nickname: string | null;
  bed_temp: number | null;
  bed_target_temp: number | null;
  nozzle_temp: number | null;
  nozzle_target_temp: number | null;
  gcode_state: string | null;
  print_stage: string | null;
  hms_errors: string | null;
  lights_on: boolean | null;
  nozzle_type: string | null;
  nozzle_diameter: number | null;
  fan_speed: number | null;
  bed_x_mm: number | null;
  bed_y_mm: number | null;
  last_seen: string | null;
  total_print_hours: number | null;
  total_print_count: number | null;
  hours_since_maintenance: number | null;
  prints_since_maintenance: number | null;
  last_error_code: string | null;
  last_error_message: string | null;
  last_error_at: string | null;
  camera_discovered: boolean | null;
  tags: string[];
  timelapse_enabled: boolean;
  machine_type: string | null;
  filament_slots: FilamentSlot[];
  loaded_colors: string[];
  shared: boolean;
  org_id: number | null;
  has_api_key: boolean;
  created_at: string;
  updated_at: string;
}

export interface PrinterCreate {
  name: string;
  model?: string;
  slot_count?: number;
  is_active?: boolean;
  api_type?: string;
  api_host?: string;
  api_key?: string;
  camera_url?: string;
  nickname?: string;
  tags?: string[];
  timelapse_enabled?: boolean;
  shared?: boolean;
  initial_slots?: Array<{
    slot_number: number;
    filament_type?: FilamentType;
    color?: string;
    color_hex?: string;
  }>;
}

export interface PrinterUpdate {
  name?: string;
  model?: string;
  slot_count?: number;
  is_active?: boolean;
  api_type?: string;
  api_host?: string;
  api_key?: string;
  camera_url?: string;
  nickname?: string;
  tags?: string[];
  timelapse_enabled?: boolean;
  shared?: boolean;
  bed_x_mm?: number;
  bed_y_mm?: number;
}

export interface PrinterSummary {
  id: number;
  name: string;
  model: string | null;
  is_active: boolean;
  loaded_colors: string[];
}

// ---- Nozzle Lifecycle ----

export interface NozzleInstall {
  nozzle_type?: string;
  nozzle_diameter?: number;
  notes?: string;
}

export interface NozzleLifecycle {
  id: number;
  printer_id: number;
  nozzle_type: string | null;
  nozzle_diameter: number | null;
  notes: string | null;
  installed_at: string;
  removed_at: string | null;
  print_hours_accumulated: number;
  print_count: number;
}

// ---- Telemetry ----

export interface TelemetryDataPoint {
  recorded_at: string;
  bed_temp: number | null;
  nozzle_temp: number | null;
  bed_target: number | null;
  nozzle_target: number | null;
  fan_speed: number | null;
}

export interface HmsErrorHistoryEntry {
  id: number;
  printer_id: number;
  code: string;
  message: string | null;
  severity: string;
  source: string;
  occurred_at: string;
}

// ---- Maintenance ----

export interface MaintenanceTask {
  id: number;
  name: string;
  description: string | null;
  printer_model_filter: string | null;
  interval_print_hours: number | null;
  interval_days: number | null;
  estimated_cost: number;
  estimated_downtime_min: number;
  is_active: boolean;
  created_at: string;
}

export interface MaintenanceTaskCreate {
  name: string;
  description?: string;
  printer_model_filter?: string;
  interval_print_hours?: number;
  interval_days?: number;
  estimated_cost?: number;
  estimated_downtime_min?: number;
  is_active?: boolean;
}

export interface MaintenanceTaskUpdate {
  name?: string;
  description?: string;
  printer_model_filter?: string;
  interval_print_hours?: number;
  interval_days?: number;
  estimated_cost?: number;
  estimated_downtime_min?: number;
  is_active?: boolean;
}

export interface MaintenanceLog {
  id: number;
  printer_id: number;
  task_id: number | null;
  task_name: string;
  performed_by: string | null;
  notes: string | null;
  cost: number;
  downtime_minutes: number;
  print_hours_at_service: number;
  performed_at: string;
}

export interface MaintenanceLogCreate {
  printer_id: number;
  task_id?: number;
  task_name: string;
  performed_by?: string;
  notes?: string;
  cost?: number;
  downtime_minutes?: number;
  print_hours_at_service?: number;
}

// ---- Smart Plug ----

export interface PlugConfig {
  plug_type: string;
  plug_host: string;
  plug_username?: string;
  plug_password?: string;
  auto_on?: boolean;
  auto_off?: boolean;
  off_delay_minutes?: number;
}

export interface PlugEnergyData {
  current_power_w: number | null;
  today_kwh: number | null;
  month_kwh: number | null;
  voltage: number | null;
  current_a: number | null;
}

export interface PlugState {
  is_on: boolean;
  relay_state: number;
}

// ---- AMS Environment ----

export interface AmsEnvironmentData {
  history: Array<{
    recorded_at: string;
    temperature: number | null;
    humidity: number | null;
    unit: number | null;
  }>;
}

export interface AmsCurrentData {
  units: Array<{
    unit: number;
    temperature: number | null;
    humidity: number | null;
  }>;
}

// ---- Connection Test ----

export interface ConnectionTestRequest {
  api_type: string;
  api_host: string;
  api_key?: string;
}

export interface ConnectionTestResult {
  success: boolean;
  message: string;
  printer_info?: Record<string, unknown>;
}

// ---- Camera ----

export interface Camera {
  id: number;
  printer_id: number;
  url: string;
  name: string | null;
  type: string | null;
}
