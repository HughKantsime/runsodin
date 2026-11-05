import { useEffect, useRef, useState } from 'react'

/** Same-origin cookies and camera RBAC protect both fallback transports. */
export type FallbackState = { transport: 'mjpeg' | 'snapshot'; live: boolean; failed: boolean }
export default function CameraFallback({ cameraId, active, onState, hideUnavailable = false }: { cameraId: number; active: boolean; hideUnavailable?: boolean; onState?: (state: FallbackState) => void }) {
  return active && cameraId ? <FallbackStream key={cameraId} cameraId={cameraId} onState={onState} hideUnavailable={hideUnavailable} /> : null
}

function FallbackStream({ cameraId, onState, hideUnavailable }: { cameraId: number; hideUnavailable: boolean; onState?: (state: FallbackState) => void }) {
  const image = useRef<HTMLImageElement>(null)
  const [mode, setMode] = useState<'mjpeg' | 'snapshot'>('mjpeg')
  const [refreshing, setRefreshing] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const [live, setLive] = useState(false)
  const [stamp, setStamp] = useState(Date.now)
  const [failed, setFailed] = useState(false)
  useEffect(() => { onState?.({ transport: mode, live: mode === 'mjpeg' && live && !failed, failed }) }, [mode, live, failed, onState])
  useEffect(() => {
    if (mode === 'mjpeg') {
      if (live) return
      const timeout = setTimeout(() => {
        // A decoded first frame counts even if the multipart load event is delayed.
        if (image.current?.naturalWidth && image.current?.naturalHeight) { setLive(true); setLoaded(true) }
        else setMode('snapshot')
      }, 5000)
      return () => clearTimeout(timeout)
    }
    setStamp(Date.now())
    const timer = setInterval(() => { setStamp(Date.now()); if (hideUnavailable) { setRefreshing(true); setLoaded(false) } }, 5000)
    return () => clearInterval(timer)
  }, [mode, live, hideUnavailable])
  const src = mode === 'mjpeg' ? `/api/cameras/${cameraId}/mjpeg` : `/api/cameras/${cameraId}/snapshot?t=${stamp}`
  return <div className="absolute inset-0 bg-black z-10">
    {(!hideUnavailable || !failed || refreshing) && <img ref={image} key={mode} src={src} alt={mode === 'mjpeg' ? 'Live camera video' : 'Camera snapshot'}
      className="w-full h-full object-contain" style={hideUnavailable && !loaded ? { visibility: 'hidden' } : undefined}
      onLoad={() => { setRefreshing(false); setLoaded(true); if (mode === 'mjpeg') setLive(true); setFailed(false) }}
      onError={() => { setRefreshing(false); setLoaded(false); if (mode === 'mjpeg') setMode('snapshot'); else setFailed(true) }} />}
    {!hideUnavailable && <p role="status" className="absolute bottom-0 inset-x-0 bg-black/80 text-white text-xs p-2">
      {mode === 'mjpeg' ? (live ? 'Live video (MJPEG) over this connection' : 'Connecting to live video (MJPEG)…')
        : failed ? 'Camera snapshots unavailable. Check the camera connection.'
          : 'Live video unavailable on this connection. Showing snapshots every 5 seconds.'}
    </p>}
  </div>
}
