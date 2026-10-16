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
  runtime_status?: { state: string; reported_at: number | null };
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

export interface VisionMonitorStatus { max_threads: number; reported: boolean; active_threads: number | null; capped: boolean; capped_count: number; capped_printer_ids: number[] }

export const vision = {
  monitorStatus: (): Promise<VisionMonitorStatus> => fetchAPI('/vision/monitor-status'),
  getDetections: (params: VisionDetectionListParams): Promise<{ items: VisionDetection[]; total: number }> => fetchAPI('/vision/detections?' + new URLSearchParams(params as Record<string, string>)),
  getDetection: (id: number): Promise<VisionDetection> => fetchAPI('/vision/detections/' + id),
  reviewDetection: (id: number, status: string): Promise<VisionDetection> => fetchAPI(`/vision/detections/${id}`, { method: 'PATCH', body: JSON.stringify({ status }) }),
  // v1.8.8: one-click "false alarm, resume print" — dismisses the
  // detection AND resumes the paused printer in one request.
  // Idempotent both ways (already-dismissed / already-running return
  // success with an `actions` array describing what was a no-op).
  dismissAndResume: (id: number): Promise<{ detection_id: number; status: string; actions: string[] }> =>
    fetchAPI(`/vision/detections/${id}/dismiss-and-resume`, { method: 'POST' }),
  getSettings: (): Promise<VisionSettings> => fetchAPI('/vision/settings'),
  updateSettings: (data: Partial<VisionSettings>): Promise<VisionSettings> => fetchAPI('/vision/settings', { method: 'PATCH', body: JSON.stringify(data) }),
  getPrinterSettings: (id: number): Promise<VisionSettings> => fetchAPI(`/printers/${id}/vision`),
  updatePrinterSettings: (id: number, data: Partial<VisionSettings>): Promise<VisionSettings> => fetchAPI(`/printers/${id}/vision`, { method: 'PATCH', body: JSON.stringify(data) }),
  getModels: (): Promise<VisionModel[]> => fetchAPI('/vision/models'),
  activateModel: (id: number): Promise<VisionModel> => fetchAPI(`/vision/models/${id}/activate`, { method: 'PATCH' }),
  uploadModel: async (file: File, name: string, detectionType: string, inputSize = 640, contract?: Record<string, unknown>): Promise<VisionModel> => {
    const formData = new FormData()
    formData.append('file', file)
    const params = new URLSearchParams({ name, detection_type: detectionType, input_size: String(inputSize) })
    if (contract) params.set('inference_contract', JSON.stringify(contract))
    const response = await fetch(`${API_BASE}/vision/models?${params}`, {
      method: 'POST',
      credentials: 'include',
      body: formData,
    })
    if (!response.ok) {
      const error = new Error(response.status === 503 ? 'Model registration unconfirmed' : 'Model upload failed') as Error & { status: number }
      error.status = response.status
      throw error
    }
    return response.json()
  },
  getStats: (days?: number): Promise<VisionStats> => fetchAPI('/vision/stats?days=' + (days || 7)),
}

export interface TrainingFrame { id: number; printer_id: number; frame_path: string; detection_type: string; confidence: number; status: string; bbox_json: string | null; ownership: 'unknown'; maintenance_only: true; annotation: { revision: number; class: string; negative: boolean; bbox: number[]; image_sha256: string } | null }
export interface ViewedAnnotation { expected_revision: number; expected_image_sha256: string; class: string; negative: boolean; bbox: number[] }
export const training = {
  getFrame: async (id: number): Promise<TrainingFrame> => {
    const row = await vision.getDetection(id)
    if (!row.frame_path) throw new Error('This detection has no retained frame')
    let metadata: Record<string, any> = {}
    if (row.metadata_json) {
      if (row.metadata_json.length > 65536) throw new Error('Frame metadata exceeds the limit')
      metadata = JSON.parse(row.metadata_json)
      if (!metadata || Array.isArray(metadata) || typeof metadata !== 'object') throw new Error('Frame metadata is invalid')
    }
    return { ...row, frame_path: row.frame_path, annotation: metadata.training_annotation || null, ownership: 'unknown', maintenance_only: true }
  },
  list: (offset: number, labeled?: boolean): Promise<TrainingFrame[]> => fetchAPI('/vision/training-data?' + new URLSearchParams({ limit: '25', offset: String(offset), ...(labeled === undefined ? {} : { labeled: String(labeled) }) })),
  annotate: (id: number, body: ViewedAnnotation): Promise<{ annotation_revision: number }> => fetchAPI(`/vision/training-data/${id}/annotation-v2`, { method: 'POST', body: JSON.stringify(body) }),
}
