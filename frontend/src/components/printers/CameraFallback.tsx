import { useEffect, useRef, useState } from 'react'

/** Same-origin cookies and camera RBAC protect both fallback transports. */
export default function CameraFallback({ cameraId, active }: { cameraId: number; active: boolean }) {
  return active && cameraId ? <FallbackStream key={cameraId} cameraId={cameraId} /> : null
}

function FallbackStream({ cameraId }: { cameraId: number }) {
  const image = useRef<HTMLImageElement>(null)
  const [mode, setMode] = useState<'mjpeg' | 'snapshot'>('mjpeg')
  const [live, setLive] = useState(false)
  const [stamp, setStamp] = useState(Date.now)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    if (mode === 'mjpeg') {
      if (live) return
      const timeout = setTimeout(() => {
        // A decoded first frame counts even if the multipart load event is delayed.
        if (image.current?.naturalWidth && image.current?.naturalHeight) setLive(true)
        else setMode('snapshot')
      }, 5000)
      return () => clearTimeout(timeout)
    }
    setStamp(Date.now())
    const timer = setInterval(() => setStamp(Date.now()), 5000)
    return () => clearInterval(timer)
  }, [mode, live])
  const src = mode === 'mjpeg' ? `/api/cameras/${cameraId}/mjpeg` : `/api/cameras/${cameraId}/snapshot?t=${stamp}`
  return <div className="absolute inset-0 bg-black z-10">
    <img ref={image} key={mode} src={src} alt={mode === 'mjpeg' ? 'Live camera video' : 'Camera snapshot'}
      className="w-full h-full object-contain"
      onLoad={() => { if (mode === 'mjpeg') setLive(true); setFailed(false) }}
      onError={() => { if (mode === 'mjpeg') setMode('snapshot'); else setFailed(true) }} />
    <p role="status" className="absolute bottom-0 inset-x-0 bg-black/80 text-white text-xs p-2">
      {mode === 'mjpeg' ? (live ? 'Live video (MJPEG) over this connection' : 'Connecting to live video (MJPEG)…')
        : failed ? 'Camera snapshots unavailable. Check the camera connection.'
          : 'Live video unavailable on this connection. Showing snapshots every 5 seconds.'}
    </p>
  </div>
}
