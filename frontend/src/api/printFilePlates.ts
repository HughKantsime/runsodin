import { fetchAPI } from './client'

// GET /api/print-files/{id}/plates (facts) and /plates/{n}/thumbnail (one picture).
// null warnings = the slicer did not report them ("not available"); [] = "none recorded".
export interface PlateFilament { slot: number; type: string; color: string | null; grams: number | null }
export interface PlateWarning { message: string | null; level: string | null; code: string | null }
export interface PlateFacts {
  index: number
  has_thumbnail: boolean
  filaments: PlateFilament[]
  print_time_seconds: number | null
  weight_grams: number | null
  layer_height: number | null
  warnings: PlateWarning[] | null
}
export interface PrintFilePlates {
  status: "available"
  reason: null
  version: number
  file_hash: string
  slicer: { name: string | null; version: string | null } | null
  plate_count: number
  plates: PlateFacts[]
}

export const unavailableReasons = {
  no_slicer_facts: 'This G-code file has no recorded slice details.',
  binary_gcode: 'This binary G-code file has no readable slice summary.',
  unsupported_type: 'This file type does not contain supported slice facts.',
  not_sliced: 'This project has no sliced plates. Export a sliced 3MF to see slice facts.',
  file_missing: 'The stored print file is no longer available.',
  missing_hash: 'This file has no recorded fingerprint.',
  hash_mismatch: 'The stored file has changed since it was added.',
  parse_refused: 'The file could not be read within the supported file limits.',
  parse_failed: 'The slice details could not be read.',
}
type FactsResult = PrintFilePlates | { status: 'not_available'; file_hash: string | null; reason: keyof typeof unavailableReasons }

export const printFilePlates = {
  completedJobs: (fileId: number) => fetchAPI<{ id: number; printer_name: string; actual_end: string | null; plate_index: number | null; ams_mapping: number[] | null }[]>(`/print-files/${fileId}/completed-jobs`),
  // Visible files without facts get a reason. A busy analysis worker returns 429.
  facts: (fileId: number, signal?: AbortSignal): Promise<FactsResult> =>
    fetchAPI<FactsResult>(`/print-files/${fileId}/plates`, { signal, quiet: true }),
}

const MAX_PLATE_THUMBNAIL_BYTES = 262144

// The authenticated thumbnail route as a blob; any non-image or non-200 answer is "no picture".
export async function fetchPlateThumbnail(fileId: number, plate: number, signal?: AbortSignal): Promise<Blob | null> {
  let response: Response
  try {
    response = await fetch(`/api/print-files/${fileId}/plates/${plate}/thumbnail`, { credentials: 'include', cache: 'no-store', signal })
  } catch {
    return null
  }
  if (!response.ok) return null
  const mediaType = (response.headers.get('Content-Type') || '').split(';')[0].trim().toLowerCase()
  if (mediaType !== 'image/png' && mediaType !== 'image/jpeg') return null
  try {
    const blob = await response.blob()
    return blob.size > 0 && blob.size <= MAX_PLATE_THUMBNAIL_BYTES ? blob : null
  } catch {
    return null
  }
}
