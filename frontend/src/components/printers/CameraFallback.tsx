import { useEffect, useState } from 'react'

/** Same-origin cookies and existing camera RBAC protect every snapshot. */
export default function CameraFallback({ cameraId, active }: { cameraId: number; active: boolean }) {
  const [src, setSrc] = useState('')
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    if (!active || !cameraId) return
    setFailed(false)
    const refresh = () => setSrc(`/api/cameras/${cameraId}/snapshot?t=${Date.now()}`)
    refresh()
    const timer = setInterval(refresh, 5000)
    return () => clearInterval(timer)
  }, [cameraId, active])
  if (!active) return null
  return <div className="absolute inset-0 bg-black z-10">
    {src && <img src={src} alt="Camera snapshot" className="w-full h-full object-contain" onLoad={() => setFailed(false)} onError={() => setFailed(true)} />}
    <p role="status" className="absolute bottom-0 inset-x-0 bg-black/80 text-white text-xs p-2">
      {failed ? 'Camera snapshots unavailable. Check the camera connection.' : 'Live video unavailable on this connection. Showing snapshots every 5 seconds.'}
    </p>
  </div>
}
