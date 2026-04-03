import { fetchAPI } from './client'
import type { FilamentType } from '../types'

const API_BASE = '/api'

// ---- Model types (defined here to avoid circular deps with job types) ----

export interface ColorRequirement {
  color: string;
  grams: number;
  filament_type?: FilamentType;
}

export interface Model {
  id: number;
  name: string;
  build_time_hours: number | null;
  default_filament_type: FilamentType;
  color_requirements: Record<string, ColorRequirement> | null;
  category: string | null;
  thumbnail_url: string | null;
  thumbnail_b64: string | null;
  notes: string | null;
  cost_per_item: number | null;
  units_per_bed: number | null;
  quantity_per_bed: number | null;
  markup_percent: number | null;
  is_favorite: boolean;
  created_at: string;
  updated_at: string;
  required_colors: string[];
  total_filament_grams: number;
  time_per_item: number | null;
  filament_per_item: number | null;
  value_per_bed: number | null;
  value_per_hour: number | null;
}

export interface ModelCreate {
  name: string;
  build_time_hours?: number;
  default_filament_type?: FilamentType;
  color_requirements?: Record<string, ColorRequirement>;
  category?: string;
  thumbnail_url?: string;
  thumbnail_b64?: string;
  notes?: string;
  cost_per_item?: number;
  units_per_bed?: number;
  quantity_per_bed?: number;
  markup_percent?: number;
  is_favorite?: boolean;
}

export interface ModelUpdate {
  name?: string;
  build_time_hours?: number;
  default_filament_type?: FilamentType;
  color_requirements?: Record<string, ColorRequirement>;
  category?: string;
  thumbnail_url?: string;
  notes?: string;
  cost_per_item?: number;
  units_per_bed?: number;
  quantity_per_bed?: number;
  markup_percent?: number;
  is_favorite?: boolean;
}

export interface ModelVariant {
  id: number;
  model_id: number;
  name: string;
  [key: string]: unknown;
}

export interface ModelRevision {
  id: number;
  model_id: number;
  revision_number: number;
  changelog: string;
  created_at: string;
  [key: string]: unknown;
}

export interface ModelCostResult {
  filament_cost: number;
  energy_cost: number;
  labor_cost: number;
  total_cost: number;
  suggested_price: number;
  [key: string]: unknown;
}

export interface ModelWithPricing extends Model {
  suggested_price?: number;
  estimated_cost?: number;
}

// ---- Slicer Profile types ----

export interface SlicerProfile {
  id: number;
  name: string;
  slicer: string;
  category: string | null;
  printer_id: number | null;
  filament_type: string | null;
  settings: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  [key: string]: unknown;
}

export interface SlicerProfileCreate {
  name: string;
  slicer: string;
  category?: string;
  printer_id?: number;
  filament_type?: string;
  settings?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface ProfileListParams {
  slicer?: string;
  category?: string;
  printer_id?: number | string;
  filament_type?: string;
  search?: string;
  page?: number;
}

export const models = {
  getVariants: (id: number): Promise<ModelVariant[]> => fetchAPI(`/models/${id}/variants`),
  deleteVariant: (modelId: number, variantId: number): Promise<void> => fetchAPI(`/models/${modelId}/variants/${variantId}`, { method: 'DELETE' }),
  list: (orgId: number | null = null): Promise<Model[]> => fetchAPI('/models' + (orgId != null && typeof orgId !== 'object' ? '?org_id=' + orgId : '')),
  listWithPricing: (category?: string, orgId: number | null = null): Promise<ModelWithPricing[]> => fetchAPI('/models-with-pricing' + (category ? '?category=' + category : '') + (orgId != null ? (category ? '&' : '?') + 'org_id=' + orgId : '')),
  get: (id: number): Promise<Model> => fetchAPI('/models/' + id),
  create: (data: ModelCreate): Promise<Model> => fetchAPI('/models', { method: 'POST', body: JSON.stringify(data) }),
  update: (id: number, data: ModelUpdate): Promise<Model> => fetchAPI('/models/' + id, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id: number): Promise<void> => fetchAPI('/models/' + id, { method: 'DELETE' }),
  schedule: (id: number, printerId?: number): Promise<unknown> => fetchAPI('/models/' + id + '/schedule' + (printerId ? '?printer_id=' + printerId : ''), { method: 'POST' }),
}

// Model Versioning
export const modelRevisions = {
  list: (modelId: number): Promise<ModelRevision[]> => fetchAPI(`/models/${modelId}/revisions`),
  create: async (modelId: number, changelog: string, file?: File): Promise<ModelRevision> => {
    const formData = new FormData()
    formData.append('changelog', changelog)
    if (file) formData.append('file', file)
    const res = await fetch(`${API_BASE}/models/${modelId}/revisions`, {
      method: 'POST',
      credentials: 'include',
      body: formData,
    })
    if (!res.ok) throw new Error('Failed to create revision')
    return res.json()
  },
  revert: (modelId: number, revNumber: number): Promise<unknown> => fetchAPI(`/models/${modelId}/revisions/${revNumber}/revert`, { method: 'POST' }),
}

// Model Cost Calculator
export const modelCost = {
  calculate: (modelId: number): Promise<ModelCostResult> => fetchAPI(`/models/${modelId}/cost`),
}

// ============== Profiles ==============
export const profilesApi = {
  list: (params: ProfileListParams = {}): Promise<SlicerProfile[]> => {
    const q = new URLSearchParams()
    if (params.slicer) q.set('slicer', params.slicer)
    if (params.category) q.set('category', params.category)
    if (params.printer_id) q.set('printer_id', String(params.printer_id))
    if (params.filament_type) q.set('filament_type', params.filament_type)
    if (params.search) q.set('search', params.search)
    if (params.page) q.set('page', String(params.page))
    const qs = q.toString()
    return fetchAPI(`/profiles${qs ? '?' + qs : ''}`)
  },
  get: (id: number): Promise<SlicerProfile> => fetchAPI(`/profiles/${id}`),
  create: (data: SlicerProfileCreate): Promise<SlicerProfile> => fetchAPI('/profiles', { method: 'POST', body: JSON.stringify(data) }),
  update: (id: number, data: Partial<SlicerProfileCreate>): Promise<SlicerProfile> => fetchAPI(`/profiles/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  delete: (id: number): Promise<void> => fetchAPI(`/profiles/${id}`, { method: 'DELETE' }),
  exportUrl: (id: number): string => `/api/profiles/${id}/export`,
  apply: (id: number, printerId: number): Promise<unknown> => fetchAPI(`/profiles/${id}/apply`, {
    method: 'POST', body: JSON.stringify({ printer_id: printerId }),
  }),
  import: async (file: File): Promise<SlicerProfile> => {
    const formData = new FormData()
    formData.append('file', file)
    const res = await fetch('/api/profiles/import', { method: 'POST', body: formData, credentials: 'include' })
    if (!res.ok) {
      const err = await res.json().catch(() => ({}))
      throw new Error(err.detail || 'Import failed')
    }
    return res.json()
  },
}
