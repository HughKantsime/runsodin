import { useEffect, useState } from 'react'
import { fetchCurrentPlateThumbnail } from '../api/plateThumbnail'

/**
 * Object URL of the running print's plate picture, or null (disabled, not available, 404).
 * Refetched when `runKey` changes (a different running print); the URL is revoked when it is
 * replaced and on unmount, and nothing is requested while `enabled` is false.
 */
export function usePlatePicture(printerId: number, enabled: boolean, runKey: string): string | null {
  const [url, setUrl] = useState<string | null>(null)
  useEffect(() => {
    setUrl(null)
    if (!enabled) return
    const controller = new AbortController()
    let cancelled = false
    let objectUrl: string | null = null
    fetchCurrentPlateThumbnail(printerId, controller.signal).then(blob => {
      if (cancelled || !blob) return
      objectUrl = URL.createObjectURL(blob)
      setUrl(objectUrl)
    }).catch(() => {})
    return () => {
      cancelled = true
      controller.abort()
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [printerId, enabled, runKey])
  return url
}
