import { useState, useEffect, useRef, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { VideoOff, X, AlertTriangle, ChevronLeft, ChevronRight, Loader2 } from 'lucide-react'
import { useBranding } from '../BrandingContext'
import { printers as printersApi, alerts as alertsApi } from '../api'
import { ONLINE_THRESHOLD_MS } from '../utils/shared'

const API_BASE = '/api'
const API_KEY = import.meta.env.VITE_API_KEY
const CARDS_PER_PAGE = 12

function TVCameraStream({ cameraId }) {
  const videoRef = useRef(null)
  const pcRef = useRef(null)
  const [status, setStatus] = useState('connecting')

  useEffect(() => {
    startWebRTC()
    return () => {
      if (pcRef.current) { pcRef.current.close(); pcRef.current = null }
    }
  }, [cameraId])

  const startWebRTC = async () => {
    try {
      setStatus('connecting')
      const pc = new RTCPeerConnection({ iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] })
      pcRef.current = pc
      pc.ontrack = (event) => {
        if (videoRef.current) { videoRef.current.srcObject = event.streams[0]; setStatus('live') }
      }
      pc.oniceconnectionstatechange = () => {
        if (pc.iceConnectionState === 'failed' || pc.iceConnectionState === 'disconnected') setStatus('disconnected')
      }
      pc.addTransceiver('video', { direction: 'recvonly' })
      const offer = await pc.createOffer()
      await pc.setLocalDescription(offer)
      
      const headers = { 'Content-Type': 'application/sdp', 'X-API-Key': API_KEY }
      
      const response = await fetch(`${API_BASE}/cameras/${cameraId}/webrtc`, { method: 'POST', headers, body: offer.sdp })
      if (!response.ok) throw new Error('Failed')
      const answerSDP = await response.text()
      await pc.setRemoteDescription(new RTCSessionDescription({ type: 'answer', sdp: answerSDP }))
    } catch {
      setStatus('error')
    }
  }

  return (
    <div className="relative w-full h-full bg-black">
      <video ref={videoRef} autoPlay playsInline muted className="w-full h-full object-contain" />
      {status === 'connecting' && (
        <div className="absolute inset-0 flex items-center justify-center">
          <div className="text-farm-600 text-xs animate-pulse">Connecting...</div>
        </div>
      )}
      {status === 'error' && (
        <div className="absolute inset-0 flex items-center justify-center">
          <VideoOff size={24} className="text-farm-700" />
        </div>
      )}
      {status === 'live' && (
        <div className="absolute top-1.5 right-1.5">
          <div className="w-1.5 h-1.5 bg-green-500 rounded-full animate-pulse" />
        </div>
      )}
    </div>
  )
}

function TVPrinterCard({ printer }) {
  const online = printer.last_seen && (Date.now() - new Date(printer.last_seen + 'Z').getTime()) < ONLINE_THRESHOLD_MS
  const isPrinting = printer.gcode_state === 'RUNNING' || printer.gcode_state === 'PAUSE'
  const hasError = printer.hms_errors && printer.hms_errors.length > 0
  const hasCamera = printer.camera_url && printer.camera_enabled !== false
  const progress = printer.mc_percent || 0
  const remaining = printer.mc_remaining_time
  const jobName = printer.gcode_file

  const formatTime = (minutes) => {
    if (!minutes) return ''
    if (minutes < 60) return `${Math.round(minutes)}m`
    return `${Math.floor(minutes / 60)}h ${Math.round(minutes % 60)}m`
  }

  const statusColor = hasError ? 'bg-red-500' : isPrinting ? 'bg-green-500' : online ? 'bg-yellow-500' : 'bg-farm-600'
  const statusLabel = hasError ? 'Error' : isPrinting ? 'Printing' : online ? 'Idle' : 'Offline'

  return (
    <div className="bg-farm-900 rounded-xl border border-farm-800 overflow-hidden flex flex-col">
      {/* Camera thumbnail */}
      {hasCamera ? (
        <div className="aspect-video">
          <TVCameraStream cameraId={printer.id} />
        </div>
      ) : (
        <div className="aspect-video bg-farm-950 flex items-center justify-center">
          <VideoOff size={32} className="text-farm-800" />
        </div>
      )}

      {/* Printer info */}
      <div className="p-3 flex-1 flex flex-col">
        <div className="flex items-center justify-between mb-2">
          <h3 className="font-display font-bold text-lg truncate">{printer.nickname || printer.name}</h3>
          <div className="flex items-center gap-1.5 flex-shrink-0">
            <div className={`w-2.5 h-2.5 rounded-full ${statusColor}`} />
            <span className="text-sm text-farm-400">{statusLabel}</span>
          </div>
        </div>

        {isPrinting && (
          <div className="mt-auto">
            {jobName && <p className="text-sm text-farm-400 truncate mb-2">{jobName}</p>}
            <div className="flex items-center justify-between mb-1.5">
              <span className="text-2xl font-bold text-green-400 tabular-nums">{progress}%</span>
              {remaining && <span className="text-sm text-farm-500">ETA {formatTime(remaining)}</span>}
            </div>
            <div className="w-full bg-farm-700 rounded-full h-3">
              <div className="bg-green-500 h-3 rounded-full transition-all duration-500" style={{ width: `${progress}%` }} />
            </div>
          </div>
        )}

        {hasError && !isPrinting && (
          <div className="mt-auto flex items-center gap-2 text-red-400 text-sm">
            <AlertTriangle size={14} />
            <span className="truncate">{printer.hms_errors?.[0]?.code || 'Error'}</span>
          </div>
        )}
      </div>
    </div>
  )
}

export default function TVDashboard() {
  const navigate = useNavigate()
  const branding = useBranding()
  const [currentTime, setCurrentTime] = useState(new Date())
  const [page, setPage] = useState(0)

  // Clock
  useEffect(() => {
    const interval = setInterval(() => setCurrentTime(new Date()), 1000)
    return () => clearInterval(interval)
  }, [])

  // Escape to exit
  useEffect(() => {
    const handleKey = (e) => {
      if (e.key === 'Escape') navigate('/')
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [navigate])

  const autoPageTimerRef = useRef(null)

  // Data
  const { data: printerList, isLoading: printersLoading } = useQuery({
    queryKey: ['printers'],
    queryFn: () => printersApi.list(true),
    refetchInterval: 15000,
  })

  const { data: alertSummary } = useQuery({
    queryKey: ['alert-summary-tv'],
    queryFn: alertsApi.summary,
    refetchInterval: 60000,
  })

  const allPrinters = printerList || []
  const totalPages = Math.max(1, Math.ceil(allPrinters.length / CARDS_PER_PAGE))
  const printingCount = allPrinters.filter(p => p.gcode_state === 'RUNNING' || p.gcode_state === 'PAUSE').length
  const errorCount = allPrinters.filter(p => p.hms_errors?.length > 0).length
  const unreadAlerts = alertSummary?.unread_count || 0

  // Reset and restart auto-pagination timer
  const resetAutoPage = useCallback(() => {
    if (autoPageTimerRef.current) clearInterval(autoPageTimerRef.current)
    if (totalPages <= 1) return
    autoPageTimerRef.current = setInterval(() => {
      setPage(p => (p + 1) % totalPages)
    }, 30000)
  }, [totalPages])

  // Auto-pagination
  useEffect(() => {
    resetAutoPage()
    return () => { if (autoPageTimerRef.current) clearInterval(autoPageTimerRef.current) }
  }, [resetAutoPage])

  // Reset page if it's out of bounds
  useEffect(() => {
    if (page >= totalPages) setPage(0)
  }, [page, totalPages])

  const visiblePrinters = allPrinters.slice(page * CARDS_PER_PAGE, (page + 1) * CARDS_PER_PAGE)

  return (
    <div className="fixed inset-0 bg-farm-950 flex flex-col">
      {/* Header */}
      <div className="flex items-center justify-between px-6 py-3 border-b border-farm-800 flex-shrink-0">
        <div className="flex items-center gap-6">
          <h1 className="font-display font-bold text-xl" style={{ color: 'var(--brand-accent, #6d8af0)' }}>
            {branding.app_name || 'O.D.I.N.'}
          </h1>
          <div className="flex items-center gap-4 text-sm">
            <span className="text-green-400 font-medium">{printingCount}/{allPrinters.length} Printing</span>
            {errorCount > 0 && <span className="text-red-400 font-medium">{errorCount} Error{errorCount !== 1 ? 's' : ''}</span>}
          </div>
        </div>
        <div className="flex items-center gap-6">
          <div className="text-right">
            <div className="font-mono text-xl text-farm-200 tabular-nums">
              {currentTime.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
            </div>
            <div className="text-xs text-farm-500">
              {currentTime.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' })}
            </div>
          </div>
          <button
            onClick={() => navigate('/')}
            className="p-1.5 text-farm-600 hover:text-farm-400 transition-colors rounded-lg"
            title="Exit TV Mode (Esc)"
          >
            <X size={18} />
          </button>
        </div>
      </div>

      {/* Printer grid */}
      <div className="flex-1 overflow-hidden p-4">
        {printersLoading ? (
          <div className="flex items-center justify-center h-full">
            <Loader2 size={32} className="animate-spin text-farm-500" />
          </div>
        ) : (
          <div className="grid gap-3 h-full" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))' }}>
            {visiblePrinters.map(printer => (
              <TVPrinterCard key={printer.id} printer={printer} />
            ))}
          </div>
        )}
      </div>

      {/* Stats bar */}
      <div className="flex items-center justify-between px-6 py-2.5 border-t border-farm-800 flex-shrink-0 text-sm">
        <div className="flex items-center gap-6 text-farm-400">
          {unreadAlerts > 0 && (
            <span className="text-amber-400">{unreadAlerts} Active Alert{unreadAlerts !== 1 ? 's' : ''}</span>
          )}
          {unreadAlerts === 0 && <span className="text-farm-600">No active alerts</span>}
        </div>
        <div className="flex items-center gap-3">
          {totalPages > 1 && (
            <div className="flex items-center gap-2">
              <button onClick={() => { setPage(p => (p - 1 + totalPages) % totalPages); resetAutoPage() }} className="p-1 text-farm-600 hover:text-farm-400">
                <ChevronLeft size={16} />
              </button>
              <div className="flex items-center gap-1">
                {Array.from({ length: totalPages }).map((_, i) => (
                  <div
                    key={i}
                    className={`w-1.5 h-1.5 rounded-full transition-colors ${i === page ? 'bg-print-500' : 'bg-farm-700'}`}
                  />
                ))}
              </div>
              <button onClick={() => { setPage(p => (p + 1) % totalPages); resetAutoPage() }} className="p-1 text-farm-600 hover:text-farm-400">
                <ChevronRight size={16} />
              </button>
            </div>
          )}
          <span className="text-[10px] text-farm-700">Powered by O.D.I.N.</span>
        </div>
      </div>
    </div>
  )
}
