import { useState, useEffect, useRef, useCallback } from 'react'
import { useQuery } from '@tanstack/react-query'
import { X, Maximize2, Minimize2, Monitor, Thermometer, RefreshCw, Video } from 'lucide-react'
import { printers as printersApi } from '../../api'
import { PrinterInfoPanel, ActiveJobPanel, FilamentSlotsPanel } from './PrinterPanels'
import { Modal } from '../ui'

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

  // Custom escape handler for small and fullscreen modes only
  // (Modal handles escape for the large mode)
  useEffect(() => {
    if (size === 'large') return // Modal handles this
    const handleKey = (e) => {
      if (e.key === 'Escape') {
        if (size === 'fullscreen') setSize('large')
        else onClose()
      }
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [size, onClose])

  if (!printer) return null

  const dotColor = status === 'live'
    ? 'bg-[var(--status-completed)]'
    : status === 'connecting'
      ? 'bg-amber-500'
      : 'bg-red-500'

  const isPrinting = p.gcode_state === 'RUNNING' || p.gcode_state === 'PAUSE'
  const nozTemp = p.nozzle_temp != null ? Math.round(p.nozzle_temp) : null
  const bedTemp = p.bed_temp != null ? Math.round(p.bed_temp) : null

  const videoSection = (
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
            <Video size={32} className="text-[var(--brand-text-muted)] animate-pulse" />
            <div className="text-[var(--brand-text-secondary)] text-sm animate-pulse">Connecting...</div>
          </div>
        )}
        {(status === 'error' || status === 'disconnected') && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2">
            <div className="text-red-400 text-sm">{error || 'Connection lost'}</div>
            <button
              onClick={startWebRTC}
              className="flex items-center gap-1.5 px-3 py-1.5 bg-[var(--brand-card-bg)] hover:bg-farm-700 rounded-lg text-sm text-[var(--brand-text-secondary)] transition-colors"
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
              className="p-1.5 bg-black/40 backdrop-blur-sm rounded-md hover:bg-black/80 transition-colors"
              aria-label="Expand camera view"
            >
              <Maximize2 size={14} />
            </button>
          )}
          {size === 'large' && (
            <>
              <button
                onClick={() => setSize('small')}
                className="p-1.5 bg-black/40 backdrop-blur-sm rounded-md hover:bg-black/80 transition-colors"
                aria-label="Minimize camera view"
              >
                <Minimize2 size={14} />
              </button>
              <button
                onClick={() => setSize('fullscreen')}
                className="p-1.5 bg-black/40 backdrop-blur-sm rounded-md hover:bg-black/80 transition-colors"
                aria-label="Enter fullscreen"
              >
                <Monitor size={14} />
              </button>
            </>
          )}
          {size === 'fullscreen' && (
            <button
              onClick={() => setSize('large')}
              className="p-1.5 bg-black/40 backdrop-blur-sm rounded-md hover:bg-black/80 transition-colors"
              aria-label="Exit fullscreen"
            >
              <Minimize2 size={14} />
            </button>
          )}
          <button
            onClick={onClose}
            className="p-1.5 bg-black/40 backdrop-blur-sm rounded-md hover:bg-black/80 transition-colors"
            aria-label="Close camera"
          >
            <X size={14} />
          </button>
        </div>
      </div>
      {/* Status bar */}
      <div className={'flex items-center justify-between px-3 py-2 ' + (size === 'fullscreen' ? 'bg-black/90' : 'border-t border-[var(--brand-card-border)]')}>
        <div className="flex items-center gap-2">
          <div className={'w-1.5 h-1.5 rounded-full ' + dotColor} />
          <span className="font-medium text-sm">{p.name}</span>
        </div>
        <span className="text-xs text-[var(--brand-text-muted)] capitalize">{status}</span>
      </div>
      {/* Large mode: compact info bar — only visible when printing */}
      {size === 'large' && isPrinting && (
        <div className="flex items-center gap-4 px-3 py-2 border-t border-[var(--brand-card-border)] text-xs text-[var(--brand-text-secondary)] overflow-x-auto">
          <span className="text-[var(--brand-text-secondary)] font-medium truncate max-w-[200px]">{p.gcode_file || 'Printing'}</span>
          <span className="text-[var(--status-completed)] font-bold">{p.mc_percent || 0}%</span>
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
          {p.layer_num && p.total_layer_num && (
            <span className="whitespace-nowrap">L{p.layer_num}/{p.total_layer_num}</span>
          )}
        </div>
      )}
    </div>
  )

  // Small PIP mode
  if (size === 'small') {
    return (
      <div className="fixed bottom-4 right-4 z-50" aria-label={`Camera feed for ${p.name}`}>
        <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] shadow-xl w-96 flex flex-col">
          {videoSection}
        </div>
      </div>
    )
  }

  // Fullscreen mode
  if (size === 'fullscreen') {
    return (
      <div className="fixed inset-0 z-[60] bg-black" role="dialog" aria-modal="true" aria-label={`Camera feed for ${p.name}`}>
        <div className="w-full h-full flex flex-row">
          {videoSection}
          <div className="w-80 bg-[var(--brand-card-bg)] border-l border-[var(--brand-card-border)] p-3 overflow-y-auto space-y-3 [&>*+*]:border-t [&>*+*]:border-[var(--brand-card-border)] [&>*+*]:pt-3">
            <PrinterInfoPanel printer={p} />
            <ActiveJobPanel printer={p} />
            <FilamentSlotsPanel printer={p} />
          </div>
        </div>
      </div>
    )
  }

  // Large modal mode — use shared Modal component
  return (
    <Modal
      isOpen={true}
      onClose={onClose}
      size="xl"
      mobileSheet={false}
      className="!p-0 !max-w-5xl"
    >
      {videoSection}
    </Modal>
  )
}
