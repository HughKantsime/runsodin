import { useEffect, useState } from 'react'
import { fetchCurrentPlateThumbnail } from '../api/plateThumbnail'

const RETRY_DELAY_MS = 10000
const MAX_RETRIES = 6

/**
 * Object URL of the running print's plate picture, or null (disabled, not available, 404).
 * Missing pictures are retried for one minute to allow the background cache build to finish.
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
    let retryTimer: ReturnType<typeof setTimeout> | undefined
    let retries = 0
    const fetchPicture = () => {
      fetchCurrentPlateThumbnail(printerId, controller.signal).then(blob => {
        if (cancelled) return
        if (blob) {
          objectUrl = URL.createObjectURL(blob)
          setUrl(objectUrl)
        } else if (retries < MAX_RETRIES) {
          retries++
          retryTimer = setTimeout(fetchPicture, RETRY_DELAY_MS)
        }
      }).catch(() => {})
    }
    fetchPicture()
    return () => {
      cancelled = true
      clearTimeout(retryTimer)
      controller.abort()
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [printerId, enabled, runKey])
  return url
}
