// Inventory domain types

import type { FilamentType } from './printer';

export type SpoolStatus = 'active' | 'empty' | 'archived';

// ---- Spool ----

export interface Spool {
  id: number;
  filament_id: number;
  qr_code: string | null;
  rfid_tag: string | null;
  color_hex: string | null;
  initial_weight_g: number;
  remaining_weight_g: number;
  spool_weight_g: number;
  price: number | null;
  purchase_date: string | null;
  vendor: string | null;
  lot_number: string | null;
  status: SpoolStatus;
  location_printer_id: number | null;
  location_slot: number | null;
  storage_location: string | null;
  notes: string | null;
  pa_profile: string | null;
  low_stock_threshold_g: number;
  spoolman_spool_id: number | null;
  created_at: string;
  updated_at: string;
  percent_remaining: number;
  filament_name: string | null;
  filament_material: string | null;
  filament_brand: string | null;
  filament_color_hex: string | null;
}

export interface SpoolCreate {
  filament_id: number;
  qr_code?: string;
  rfid_tag?: string;
  color_hex?: string;
  initial_weight_g?: number;
  remaining_weight_g?: number;
  spool_weight_g?: number;
  price?: number;
  purchase_date?: string;
  vendor?: string;
  lot_number?: string;
  status?: SpoolStatus;
  location_printer_id?: number;
  location_slot?: number;
  storage_location?: string;
  notes?: string;
  pa_profile?: string;
  low_stock_threshold_g?: number;
  spoolman_spool_id?: number;
}

export interface SpoolUpdate {
  id: number;
  remaining_weight_g?: number;
  status?: SpoolStatus;
  location_printer_id?: number;
  location_slot?: number;
  storage_location?: string;
  notes?: string;
  pa_profile?: string;
  low_stock_threshold_g?: number;
  rfid_tag?: string;
  color_hex?: string;
}

export interface SpoolLoadRequest {
  id: number;
  printer_id: number;
  slot_number: number;
}

export interface SpoolUnloadRequest {
  id: number;
  storage_location?: string;
}

export interface SpoolUseRequest {
  id: number;
  weight_used_g: number;
  notes?: string;
}

export interface SpoolScanAssignRequest {
  qr_code: string;
  printer_id: number;
  slot: number;
}

export interface SpoolDryingRequest {
  duration_hours: number;
  method?: string;
  temp_c?: number;
  notes?: string;
}

export interface SpoolDryingRecord {
  id: number;
  spool_id: number;
  duration_hours: number;
  method: string;
  temp_c: number | null;
  notes: string | null;
  dried_at: string;
}

export interface SpoolListFilters {
  status?: SpoolStatus;
  printer_id?: number | string;
  org_id?: number;
}

// ---- Filament Library ----

export interface Filament {
  id: number;
  brand: string;
  name: string;
  material: string;
  color_hex: string | null;
  cost_per_gram: number | null;
  is_custom: boolean;
  created_at: string;
}

export interface FilamentCreate {
  brand: string;
  name: string;
  material?: string;
  color_hex?: string;
  cost_per_gram?: number;
  is_custom?: boolean;
}

export interface FilamentUpdate {
  id: number;
  brand?: string;
  name?: string;
  material?: string;
  color_hex?: string;
  cost_per_gram?: number;
}

// ---- Consumables ----

export interface Consumable {
  id: number;
  name: string;
  sku: string | null;
  unit: string;
  cost_per_unit: number;
  current_stock: number;
  min_stock: number;
  vendor: string | null;
  notes: string | null;
  status: string;
  created_at: string;
  updated_at: string;
  is_low_stock: boolean | null;
}

export interface ConsumableCreate {
  name: string;
  sku?: string;
  unit?: string;
  cost_per_unit?: number;
  current_stock?: number;
  min_stock?: number;
  vendor?: string;
  notes?: string;
  status?: string;
}

export interface ConsumableUpdate {
  name?: string;
  sku?: string;
  unit?: string;
  cost_per_unit?: number;
  current_stock?: number;
  min_stock?: number;
  vendor?: string;
  notes?: string;
  status?: string;
}

export interface ConsumableAdjust {
  quantity: number;
  type: 'restock' | 'deduct';
  notes?: string;
}
