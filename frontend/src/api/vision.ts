import { fetchAPI } from './client'

const API_BASE = '/api'

// ---- Vision types ----

export interface VisionDetection {
  id: number;
  printer_id: number;
  print_job_id: number | null;
  detection_type: string;
  confidence: number;
  status: string;
  frame_path: string | null;
  bbox_json: string | null;
  metadata_json: string | null;
  reviewed_by: number | null;
  reviewed_at: string | null;
  created_at: string;
}

export interface VisionSettings {
  printer_id: number;
  enabled: number;
  spaghetti_enabled: number;
  spaghetti_threshold: number;
  first_layer_enabled: number;
  first_layer_threshold: number;
  detachment_enabled: number;
  detachment_threshold: number;
  build_plate_empty_enabled: number;
  build_plate_empty_threshold: number;
  auto_pause: number;
  capture_interval_sec: number;
  collect_training_data: number;
  updated_at: string | null;
}

export interface VisionModel {
  id: number;
  name: string;
  detection_type: string;
  filename: string;
  version: string | null;
  input_size: number;
  is_active: number;
  metadata_json: string | null;
  uploaded_at: string;
}

export interface VisionStats {
  [key: string]: unknown;
}

export interface VisionDetectionListParams {
  printer_id?: number | string;
  detection_type?: string;
  status?: string;
  limit?: number;
  offset?: number;
  [key: string]: unknown;
}

export const vision = {
  getDetections: (params: VisionDetectionListParams): Promise<VisionDetection[]> => fetchAPI('/vision/detections?' + new URLSearchParams(params as Record<string, string>)),
  getDetection: (id: number): Promise<VisionDetection> => fetchAPI('/vision/detections/' + id),
  reviewDetection: (id: number, status: string): Promise<VisionDetection> => fetchAPI(`/vision/detections/${id}`, { method: 'PATCH', body: JSON.stringify({ status }) }),
  getSettings: (): Promise<VisionSettings> => fetchAPI('/vision/settings'),
  updateSettings: (data: Partial<VisionSettings>): Promise<VisionSettings> => fetchAPI('/vision/settings', { method: 'PATCH', body: JSON.stringify(data) }),
  getPrinterSettings: (id: number): Promise<VisionSettings> => fetchAPI(`/printers/${id}/vision`),
  updatePrinterSettings: (id: number, data: Partial<VisionSettings>): Promise<VisionSettings> => fetchAPI(`/printers/${id}/vision`, { method: 'PATCH', body: JSON.stringify(data) }),
  getModels: (): Promise<VisionModel[]> => fetchAPI('/vision/models'),
  activateModel: (id: number): Promise<VisionModel> => fetchAPI(`/vision/models/${id}/activate`, { method: 'PATCH' }),
  uploadModel: async (file: File, name: string, detectionType: string, inputSize = 640): Promise<VisionModel> => {
    const formData = new FormData()
    formData.append('file', file)
    const response = await fetch(`${API_BASE}/vision/models?name=${encodeURIComponent(name)}&detection_type=${detectionType}&input_size=${inputSize}`, {
      method: 'POST',
      credentials: 'include',
      body: formData,
    })
    if (!response.ok) throw new Error('Failed to upload model')
    return response.json()
  },
  getStats: (days?: number): Promise<VisionStats> => fetchAPI('/vision/stats?days=' + (days || 7)),
}
