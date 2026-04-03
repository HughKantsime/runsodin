import { useState, useEffect, useRef } from 'react'
import { useParams, useNavigate, Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowLeft, Maximize2, Minimize2, VideoOff, Eye, RefreshCw, Camera } from 'lucide-react'
import toast from 'react-hot-toast'
import { printers } from '../../api'
import { isOnline } from '../../utils/shared'
import { PrinterInfoPanel, FilamentSlotsPanel, ActiveJobPanel } from '../../components/printers/PrinterPanels'
import useWebRTC from '../../hooks/useWebRTC'

const API_BASE = '/api'

function AiIndicator({ printerId }) {
  const { data } = useQuery({
    queryKey: ['vision-settings', printerId],
    queryFn: async () => {
      const res = await fetch(`${API_BASE}/printers/${printerId}/vision`, { headers: { 'Content-Type': 'application/json' }, credentials: 'include' })
      if (!res.ok) return null
      return res.json()
    },
    refetchInterval: 60000,
    retry: false,
  })
  if (!data || !data.enabled) return null
  return (
    <div className="flex items-center gap-1 px-1.5 py-0.5 bg-purple-900/30 rounded-sm text-[10px] text-purple-300 font-medium" title="Vigil AI active">
      <Eye size={10} />
      <div className="w-1.5 h-1.5 rounded-full bg-purple-400" />
      AI
    </div>
  )
}

function WebRTCPlayer({ cameraId, className }) {
  const { videoRef, status, retry: handleRetry } = useWebRTC(cameraId)

  const handleSnapshot = () => {
    const video = videoRef.current
    if (!video || !video.videoWidth) return
    const canvas = document.createElement('canvas')
    canvas.width = video.videoWidth
    canvas.height = video.videoHeight
    canvas.getContext('2d').drawImage(video, 0, 0)
    const link = document.createElement('a')
    link.download = `camera-${cameraId}-${Date.now()}.png`
    link.href = canvas.toDataURL('image/png')
    link.click()
    toast.success('Snapshot saved')
  }

  return (
    <div className={`relative bg-black group ${className || ''}`}>
      <video ref={videoRef} autoPlay playsInline muted className="w-full h-full object-contain" />
      {status === 'connecting' && (
        <div className="absolute inset-0 flex items-center justify-center">
          <div className="text-[var(--brand-text-muted)] text-sm animate-pulse">Connecting...</div>
        </div>
      )}
      {(status === 'error' || status === 'disconnected') && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-2">
          <VideoOff size={48} className="text-[var(--brand-text-muted)]" />
          <button
            onClick={handleRetry}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-[var(--brand-card-bg)] hover:bg-[var(--brand-surface)] rounded-lg text-sm text-[var(--brand-text-secondary)] transition-colors"
          >
            <RefreshCw size={14} /> Reconnect
          </button>
        </div>
      )}
      {status === 'live' && (
        <div className="absolute top-3 left-3 flex items-center gap-1.5">
          <div className="w-1.5 h-1.5 bg-[var(--status-completed)] rounded-full" />
          <span className="text-[10px] font-mono text-white/70">LIVE</span>
        </div>
      )}
      {status === 'live' && (
        <div className="absolute top-3 right-3 opacity-0 group-hover:opacity-100 transition-opacity">
          <button
            onClick={handleSnapshot}
            className="p-1.5 backdrop-blur-sm bg-black/40 rounded-md hover:bg-black/80 transition-colors text-white/70 hover:text-white"
            title="Take snapshot"
          >
            <Camera size={16} />
          </button>
        </div>
      )}
    </div>
  )
}

export default function CameraDetail() {
  const { id } = useParams()
  const navigate = useNavigate()
  const containerRef = useRef(null)
  const [isFullscreen, setIsFullscreen] = useState(false)
  const printerId = parseInt(id)

  const { data: printerList, isLoading } = useQuery({
    queryKey: ['printers'],
    queryFn: () => printers.list(true),
    refetchInterval: 15000,
  })

  const printer = printerList?.find(p => p.id === printerId)

  // Fullscreen toggle
  const toggleFullscreen = () => {
    if (!containerRef.current) return
    if (!document.fullscreenElement) {
      containerRef.current.requestFullscreen()
    } else {
      document.exitFullscreen()
    }
  }

  useEffect(() => {
    const handler = () => setIsFullscreen(!!document.fullscreenElement)
    document.addEventListener('fullscreenchange', handler)
    return () => document.removeEventListener('fullscreenchange', handler)
  }, [])

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-12 text-[var(--brand-text-muted)] gap-2">
        <RefreshCw size={16} className="animate-spin" />Loading...
      </div>
    )
  }

  if (!printer) {
    return (
      <div className="p-4 md:p-6">
        <Link to="/cameras" className="flex items-center gap-2 text-[var(--brand-text-secondary)] hover:text-[var(--brand-text-primary)] transition-colors mb-4">
          <ArrowLeft size={18} /> Back to Cameras
        </Link>
        <div className="text-center py-12 text-[var(--brand-text-muted)]">
          <VideoOff size={40} className="mx-auto mb-4 text-[var(--brand-text-muted)]" />
          <p>Printer not found</p>
        </div>
      </div>
    )
  }

  const online = isOnline(printer)

  return (
    <div className="p-4 md:p-6">
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-3">
          <Link to="/cameras" className="flex items-center gap-1.5 text-xs text-[var(--brand-text-secondary)] hover:text-[var(--brand-text-primary)] transition-colors">
            <ArrowLeft size={16} /> Cameras
          </Link>
          <span className="text-[var(--brand-text-muted)]">/</span>
          <h1 className="text-lg font-semibold text-[var(--brand-text-primary)] truncate">{printer.nickname || printer.name}</h1>
        </div>
        <div className="flex items-center gap-2">
          <AiIndicator printerId={printerId} />
          <div className="flex items-center gap-1.5">
            <div className={`w-1.5 h-1.5 rounded-full ${online ? 'bg-[var(--status-completed)]' : 'bg-[var(--brand-text-muted)]'}`} />
            <span className={`text-xs ${online ? 'text-[var(--status-completed)]' : 'text-[var(--brand-text-muted)]'}`}>{online ? 'Online' : 'Offline'}</span>
          </div>
          <button
            onClick={toggleFullscreen}
            className="p-1.5 bg-[var(--brand-card-bg)] hover:bg-[var(--brand-surface)] rounded-lg transition-colors text-[var(--brand-text-secondary)] hover:text-white"
            title="Toggle fullscreen"
          >
            {isFullscreen ? <Minimize2 size={16} /> : <Maximize2 size={16} />}
          </button>
        </div>
      </div>

      {/* Main content */}
      <div className="flex flex-col md:flex-row gap-4">
        {/* Camera stream */}
        <div className="flex-[7] min-w-0" ref={containerRef}>
          <WebRTCPlayer cameraId={printerId} className="aspect-video rounded-none overflow-hidden border-b border-[var(--brand-card-border)]" />
        </div>

        {/* Data panels */}
        <div className="flex-[3] min-w-0 space-y-3">
          <PrinterInfoPanel printer={printer} />
          <FilamentSlotsPanel printer={printer} />
          <ActiveJobPanel printer={printer} />
        </div>
      </div>
    </div>
  )
}
