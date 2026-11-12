// The plate picture of the print running on a printer (GET /api/printers/{id}/current-plate-thumbnail).
// Same-origin session cookie, like the rest of the API client. Any non-image or non-200 answer
// (404 = no running print, no cache or outside the caller's audience) is simply "no picture".
const MAX_PLATE_THUMBNAIL_BYTES = 262144

export async function fetchCurrentPlateThumbnail(printerId: number, signal?: AbortSignal): Promise<Blob | null> {
  let response: Response
  try {
    response = await fetch(`/api/printers/${printerId}/current-plate-thumbnail`, { credentials: 'include', cache: 'no-store', signal })
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
