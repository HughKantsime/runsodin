import { useLayoutEffect, useRef, useState } from 'react'
import useWebRTC from '../../hooks/useWebRTC'
import type { Camera } from '../../types/printer'

function availableViewport() {
  const width = window.innerWidth
  let height = window.innerHeight
  // Reserve the visible bottom navigation, including its safe-area padding.
  for (const navigation of document.querySelectorAll('nav.fixed')) {
    const rect = navigation.getBoundingClientRect()
    if (rect.height > 0 && rect.width > 0 && rect.top >= 0 && rect.bottom >= window.innerHeight - 1) {
      height = Math.min(height, rect.top)
    }
  }
  return { width, height }
}

export default function CameraPipPlayer({ camera, onClose, warning }: { camera: Camera; onClose: () => void; warning?: boolean }) {
  const { videoRef, status, retry } = useWebRTC(camera.id)
  const [small, setSmall] = useState(false)
  const [viewport, setViewport] = useState(availableViewport)
  const width = Math.min(small ? 240 : 320, viewport.width)
  const height = Math.min(small ? 180 : 220, viewport.height)
  const [position, setPosition] = useState({ x: Math.max(0, viewport.width - 340), y: Math.max(0, viewport.height - 240) })
  const drag = useRef<{ id: number; x: number; y: number } | null>(null)
  const bounds = (x: number, y: number) => ({ x: Math.max(0, Math.min(viewport.width - width, x)), y: Math.max(0, Math.min(viewport.height - height, y)) })
  const displayed = bounds(position.x, position.y)
  useLayoutEffect(() => {
    const resize = () => { drag.current = null; setViewport(availableViewport()) }
    resize()
    window.addEventListener('resize', resize)
    return () => { drag.current = null; window.removeEventListener('resize', resize) }
  }, [])
  return <section aria-label="Picture-in-picture camera" className="fixed z-[9999] rounded-md overflow-hidden shadow-2xl border border-[var(--brand-card-border)] bg-black text-white" style={{ left: displayed.x, top: displayed.y, width, height }}>
    <header className="absolute top-0 left-0 right-0 z-10 flex items-center justify-between bg-black/80 p-1" style={{ touchAction: 'none' }}
      onPointerDown={event => {
        if (event.button !== 0 || (event.target as Element).closest('button')) return
        drag.current = { id: event.pointerId, x: event.clientX - displayed.x, y: event.clientY - displayed.y }
        event.currentTarget.setPointerCapture?.(event.pointerId)
      }}
      onPointerMove={event => {
        if (drag.current?.id === event.pointerId) setPosition(bounds(event.clientX - drag.current.x, event.clientY - drag.current.y))
      }}
      onPointerUp={event => { if (drag.current?.id === event.pointerId) { drag.current = null; event.currentTarget.releasePointerCapture?.(event.pointerId) } }}
      onPointerCancel={() => { drag.current = null }} onLostPointerCapture={() => { drag.current = null }}>
      <span className="truncate text-xs">{camera.name || 'Camera'}</span>
      <div className="flex gap-1">
        <button aria-label="Resize camera player" onClick={() => setSmall(value => !value)} className="px-2 py-1 text-xs">{small ? 'Larger' : 'Smaller'}</button>
        <button aria-label="Close camera player" onClick={onClose} className="px-2 py-1 text-xs">Close</button>
      </div>
    </header>
    <video ref={videoRef} autoPlay playsInline muted aria-label="Camera stream" className="h-full w-full object-contain" />
    <div role="status" className="absolute bottom-0 left-0 right-0 bg-black/80 p-1 text-xs">
      {status === 'live' ? 'LIVE' : status === 'connecting' ? 'Connecting' : status === 'disconnected' ? 'Disconnected' : 'Stream unavailable'}
      {(status === 'error' || status === 'disconnected') && <button onClick={retry} className="ml-2 underline">Retry stream</button>}
      {warning && <span className="block">Camera list unavailable. Access changes have not been checked.</span>}
    </div>
  </section>
}
