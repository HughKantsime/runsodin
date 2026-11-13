import { useEffect, useState } from 'react'
import { fetchPlateThumbnail } from '../api/printFilePlates'

/**
 * Object URL of one plate's picture (fetched with the session, like usePlatePicture), or null.
 * `loading` is true until the request settles. The URL is revoked when replaced and on unmount.
 */
export function usePlateThumbnail(fileId: number | null, plate: number, enabled: boolean): { url: string | null; loading: boolean } {
  const [state, setState] = useState<{ url: string | null; loading: boolean }>({ url: null, loading: false })
  useEffect(() => {
    setState({ url: null, loading: false })
    if (!enabled || fileId == null) return
    const controller = new AbortController()
    let cancelled = false
    let objectUrl: string | null = null
    setState({ url: null, loading: true })
    fetchPlateThumbnail(fileId, plate, controller.signal).then(blob => {
      if (cancelled) return
      if (blob) objectUrl = URL.createObjectURL(blob)
      setState({ url: objectUrl, loading: false })
    }).catch(() => { if (!cancelled) setState({ url: null, loading: false }) })
    return () => {
      cancelled = true
      controller.abort()
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [fileId, plate, enabled])
  return state
}
