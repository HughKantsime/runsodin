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
  version: number
  file_hash: string
  slicer: { name: string | null; version: string | null } | null
  plate_count: number
  plates: PlateFacts[]
}

export const printFilePlates = {
  // 404 = not available, 429 = analysis busy (error.status is set by fetchAPI).
  facts: (fileId: number, signal?: AbortSignal): Promise<PrintFilePlates> =>
    fetchAPI<PrintFilePlates>(`/print-files/${fileId}/plates`, { signal, quiet: true }),
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
