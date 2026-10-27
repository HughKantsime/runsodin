// Inventory domain types

import type { FilamentType } from './printer';

export type SpoolStatus = 'active' | 'empty' | 'archived';

export interface PurchaseInput {
  currency: 'USD' | 'EUR' | 'GBP' | 'CAD' | 'AUD' | 'JPY' | 'CHF' | 'NZD';
  price: string;
  net_weight_g: string;
}
export type PurchaseEvidence =
  | { state: 'unavailable'; can_write: false }
  | { state: 'unknown'; can_write: boolean; revision: number }
  | { state: 'recorded'; can_write: boolean; revision: number; evidence_nonce: string;
      currency: PurchaseInput['currency']; price_decimal: string; net_weight_g_decimal: string;
      weight_basis: string; provenance: string; recorded_at: string };

// ---- Spool ----

export interface Spool {
  id: number;
  color_name?: string | null
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
  /** Server flag: remaining_weight_g below this spool's low_stock_threshold_g. */
  is_low_stock?: boolean | null;
  filament_name: string | null;
  filament_material: string | null;
  filament_brand: string | null;
  filament_color_hex: string | null;
}

export interface SpoolCreate {
  purchase_evidence?: PurchaseInput;
  color_name?: string | null
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
  color_name?: string | null;
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
  status?: string;
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
  managed_material?: boolean
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
