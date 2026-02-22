import { useState, useEffect, useRef, useCallback } from 'react'
import { useQuery } from '@tanstack/react-query'
import { X, Maximize2, Minimize2, Monitor, Thermometer, RefreshCw, Video } from 'lucide-react'
import { printers as printersApi } from '../api'
import { PrinterInfoPanel, ActiveJobPanel, FilamentSlotsPanel } from './PrinterPanels'

export default function CameraModal({ printer, onClose }) {
  const videoRef = useRef(null)
  const pcRef = useRef(null)
  const [status, setStatus] = useState('connecting')
  const [error, setError] = useState(null)
  const [size, setSize] = useState('small') // small, large, fullscreen

  // Fetch fresh telemetry so the prop doesn't go stale
  const { data: freshPrinter } = useQuery({
    queryKey: ['printers'],
    queryFn: () => printersApi.list(true),
    refetchInterval: 15000,
    enabled: !!printer,
    select: (list) => list?.find(p => p.id === printer?.id),
  })

  const p = freshPrinter || printer

  const startWebRTC = useCallback(async () => {
    if (!printer) return
    try {
      if (pcRef.current) { pcRef.current.close(); pcRef.current = null }
      setStatus('connecting')
      setError(null)
      const pc = new RTCPeerConnection({
        iceServers: [{ urls: 'stun:stun.l.google.com:19302' }]
      })
      pcRef.current = pc

      pc.ontrack = (event) => {
        if (videoRef.current) {
          videoRef.current.srcObject = event.streams[0]
          setStatus('live')
        }
      }

      pc.oniceconnectionstatechange = () => {
        if (pc.iceConnectionState === 'failed' || pc.iceConnectionState === 'disconnected') {
          setStatus('disconnected')
        }
      }

      pc.addTransceiver('video', { direction: 'recvonly' })

      const offer = await pc.createOffer()
      await pc.setLocalDescription(offer)

      const response = await fetch('/api/cameras/' + printer.id + '/webrtc', {
        method: 'POST',
        headers: { 'Content-Type': 'application/sdp' },
        credentials: 'include',
        body: offer.sdp
      })

      if (!response.ok) throw new Error('WebRTC signaling failed')

      const answerSDP = await response.text()
      await pc.setRemoteDescription(new RTCSessionDescription({
        type: 'answer',
        sdp: answerSDP
      }))
    } catch (err) {
      setError(err.message)
      setStatus('error')
    }
  }, [printer])

  useEffect(() => {
    startWebRTC()
    return () => {
      if (pcRef.current) {
        pcRef.current.close()
        pcRef.current = null
      }
    }
  }, [startWebRTC])

  const containerRef = useRef(null)

  useEffect(() => {
    const handleKey = (e) => {
      if (e.key === 'Escape') {
        if (size === 'fullscreen') setSize('large')
        else onClose()
      }
      if (e.key === 'Tab' && containerRef.current) {
        const focusable = containerRef.current.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')
        if (focusable.length === 0) return
        const first = focusable[0]
        const last = focusable[focusable.length - 1]
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault()
          last.focus()
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault()
          first.focus()
        }
      }
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [size, onClose])

  if (!printer) return null

  const dotColor = status === 'live'
    ? 'bg-green-500'
    : status === 'connecting'
      ? 'bg-yellow-500 animate-pulse'
      : 'bg-red-500'

  const isPrinting = p.gcode_state === 'RUNNING' || p.gcode_state === 'PAUSE'
  const nozTemp = p.nozzle_temp != null ? Math.round(p.nozzle_temp) : null
  const bedTemp = p.bed_temp != null ? Math.round(p.bed_temp) : null

  const sizeClasses = size === 'fullscreen'
    ? 'fixed inset-0 z-[60] bg-black'
    : size === 'large'
      ? 'fixed inset-0 z-50 bg-black/80 flex items-center justify-center p-4'
      : 'fixed bottom-4 right-4 z-50'

  const containerClasses = size === 'fullscreen'
    ? 'w-full h-full flex flex-row'
    : size === 'large'
      ? 'bg-farm-950 rounded-lg border border-farm-800 w-full max-w-5xl flex flex-col'
      : 'bg-farm-950 rounded-lg border border-farm-800 shadow-2xl w-96 flex flex-col'

  return (
    <div className={sizeClasses} role="dialog" aria-modal={size !== 'small' ? 'true' : undefined} aria-label={`Camera feed for ${p.name}`} onClick={size === 'large' ? onClose : undefined}>
      <div ref={containerRef} className={containerClasses} onClick={e => e.stopPropagation()}>
        {/* Video section */}
        <div className={size === 'fullscreen' ? 'flex-1 flex flex-col min-w-0' : 'flex flex-col'}>
          <div className={size === 'fullscreen' ? 'flex-1 relative bg-black' : 'relative aspect-video bg-black rounded-t-xl overflow-hidden'}>
            <video
              ref={videoRef}
              autoPlay
              playsInline
              muted
              aria-label={`Live camera feed for ${p.name}`}
              className="w-full h-full object-contain"
            />
            {status === 'connecting' && (
              <div className="absolute inset-0 flex flex-col items-center justify-center gap-2">
                <Video size={32} className="text-farm-600 animate-pulse" />
                <div className="text-farm-400 text-sm animate-pulse">Connecting...</div>
              </div>
            )}
            {(status === 'error' || status === 'disconnected') && (
              <div className="absolute inset-0 flex flex-col items-center justify-center gap-2">
                <div className="text-red-400 text-sm">{error || 'Connection lost'}</div>
                <button
                  onClick={startWebRTC}
                  className="flex items-center gap-1.5 px-3 py-1.5 bg-farm-800 hover:bg-farm-700 rounded-lg text-sm text-farm-300 transition-colors"
                >
                  <RefreshCw size={14} /> Reconnect
                </button>
              </div>
            )}
            {/* Controls overlay - top right */}
            <div className="absolute top-2 right-2 flex items-center gap-1">
              {size === 'small' && (
                <button
                  onClick={() => setSize('large')}
                  className="p-1.5 bg-black/60 rounded-lg hover:bg-black/80 transition-colors"
                  aria-label="Expand camera view"
                >
                  <Maximize2 size={14} />
                </button>
              )}
              {size === 'large' && (
                <>
                  <button
                    onClick={() => setSize('small')}
                    className="p-1.5 bg-black/60 rounded-lg hover:bg-black/80 transition-colors"
                    aria-label="Minimize camera view"
                  >
                    <Minimize2 size={14} />
                  </button>
                  <button
                    onClick={() => setSize('fullscreen')}
                    className="p-1.5 bg-black/60 rounded-lg hover:bg-black/80 transition-colors"
                    aria-label="Enter fullscreen"
                  >
                    <Monitor size={14} />
                  </button>
                </>
              )}
              {size === 'fullscreen' && (
                <button
                  onClick={() => setSize('large')}
                  className="p-1.5 bg-black/60 rounded-lg hover:bg-black/80 transition-colors"
                  aria-label="Exit fullscreen"
                >
                  <Minimize2 size={14} />
                </button>
              )}
              <button
                onClick={onClose}
                className="p-1.5 bg-black/60 rounded-lg hover:bg-black/80 transition-colors"
                aria-label="Close camera"
              >
                <X size={14} />
              </button>
            </div>
          </div>
          {/* Status bar */}
          <div className={'flex items-center justify-between px-3 py-2 ' + (size === 'fullscreen' ? 'bg-black/90' : 'border-t border-farm-800')}>
            <div className="flex items-center gap-2">
              <div className={'w-2 h-2 rounded-full ' + dotColor} />
              <span className="font-medium text-sm">{p.name}</span>
            </div>
            <span className="text-xs text-farm-500 capitalize">{status}</span>
          </div>
          {/* Large mode: compact info bar */}
          {size === 'large' && (
            <div className="flex items-center gap-4 px-3 py-2 border-t border-farm-800 text-xs text-farm-400 overflow-x-auto">
              {isPrinting && (
                <>
                  <span className="text-farm-300 font-medium truncate max-w-[200px]">{p.gcode_file || 'Printing'}</span>
                  <span className="text-green-400 font-bold">{p.mc_percent || 0}%</span>
                </>
              )}
              {nozTemp != null && (
                <span className="flex items-center gap-1 whitespace-nowrap">
                  <Thermometer size={11} /> {nozTemp}°C
                </span>
              )}
              {bedTemp != null && (
                <span className="flex items-center gap-1 whitespace-nowrap">
                  Bed {bedTemp}°C
                </span>
              )}
              {isPrinting && p.layer_num && p.total_layer_num && (
                <span className="whitespace-nowrap">L{p.layer_num}/{p.total_layer_num}</span>
              )}
            </div>
          )}
        </div>
        {/* Fullscreen mode: right sidebar */}
        {size === 'fullscreen' && (
          <div className="w-80 bg-farm-950 border-l border-farm-800 p-3 overflow-y-auto space-y-3">
            <PrinterInfoPanel printer={p} />
            <ActiveJobPanel printer={p} />
            <FilamentSlotsPanel printer={p} />
          </div>
        )}
      </div>
    </div>
  )
}
