import { useState, useEffect, useRef, useCallback } from 'react'
import { useParams, useNavigate, Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowLeft, Maximize2, Minimize2, VideoOff, Eye, RefreshCw, Camera } from 'lucide-react'
import toast from 'react-hot-toast'
import { printers } from '../api'
import { PrinterInfoPanel, FilamentSlotsPanel, ActiveJobPanel } from '../components/PrinterPanels'

const API_BASE = '/api'
const API_KEY = import.meta.env.VITE_API_KEY

function AiIndicator({ printerId }) {
  const { data } = useQuery({
    queryKey: ['vision-settings', printerId],
    queryFn: async () => {
      
      const headers = { 'Content-Type': 'application/json', 'X-API-Key': API_KEY }
      
      const res = await fetch(`${API_BASE}/printers/${printerId}/vision`, { headers })
      if (!res.ok) return null
      return res.json()
    },
    refetchInterval: 60000,
    retry: false,
  })
  if (!data || !data.enabled) return null
  return (
    <div className="flex items-center gap-1 px-1.5 py-0.5 bg-purple-900/50 rounded-lg text-[10px] text-purple-300 font-medium" title="Vigil AI active">
      <Eye size={10} />
      <div className="w-1.5 h-1.5 rounded-full bg-purple-400 animate-pulse" />
      AI
    </div>
  )
}

function WebRTCPlayer({ cameraId, className }) {
  const videoRef = useRef(null)
  const pcRef = useRef(null)
  const retryRef = useRef(null)
  const retryCountRef = useRef(0)
  const [status, setStatus] = useState('connecting')

  const startWebRTC = useCallback(async () => {
    try {
      if (pcRef.current) { pcRef.current.close(); pcRef.current = null }
      setStatus('connecting')
      const pc = new RTCPeerConnection({ iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] })
      pcRef.current = pc
      pc.ontrack = (event) => {
        if (videoRef.current) { videoRef.current.srcObject = event.streams[0]; setStatus('live'); retryCountRef.current = 0 }
      }
      pc.oniceconnectionstatechange = () => {
        if (pc.iceConnectionState === 'failed' || pc.iceConnectionState === 'disconnected') {
          setStatus('disconnected')
          const delay = Math.min(2000 * Math.pow(2, retryCountRef.current), 30000)
          retryCountRef.current++
          retryRef.current = setTimeout(startWebRTC, delay)
        }
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
      const delay = Math.min(2000 * Math.pow(2, retryCountRef.current), 30000)
      retryCountRef.current++
      retryRef.current = setTimeout(startWebRTC, delay)
    }
  }, [cameraId])

  useEffect(() => {
    startWebRTC()
    return () => {
      clearTimeout(retryRef.current)
      if (pcRef.current) { pcRef.current.close(); pcRef.current = null }
    }
  }, [startWebRTC])

  const handleRetry = () => {
    clearTimeout(retryRef.current)
    retryCountRef.current = 0
    startWebRTC()
  }

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
          <div className="text-farm-500 text-sm animate-pulse">Connecting...</div>
        </div>
      )}
      {(status === 'error' || status === 'disconnected') && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-2">
          <VideoOff size={48} className="text-farm-600" />
          <button
            onClick={handleRetry}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-farm-800 hover:bg-farm-700 rounded-lg text-sm text-farm-300 transition-colors"
          >
            <RefreshCw size={14} /> Reconnect
          </button>
        </div>
      )}
      {status === 'live' && (
        <div className="absolute top-3 left-3 flex items-center gap-1.5">
          <div className="w-2 h-2 bg-green-500 rounded-full animate-pulse" />
          <span className="text-xs text-white/70 font-medium">LIVE</span>
        </div>
      )}
      {status === 'live' && (
        <div className="absolute top-3 right-3 opacity-0 group-hover:opacity-100 transition-opacity">
          <button
            onClick={handleSnapshot}
            className="p-1.5 bg-black/60 rounded-lg hover:bg-black/80 transition-colors text-white/70 hover:text-white"
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
      <div className="flex items-center justify-center py-12 text-farm-500 gap-2">
        <RefreshCw size={16} className="animate-spin" />Loading...
      </div>
    )
  }

  if (!printer) {
    return (
      <div className="p-4 md:p-6">
        <Link to="/cameras" className="flex items-center gap-2 text-farm-400 hover:text-white transition-colors mb-4">
          <ArrowLeft size={18} /> Back to Cameras
        </Link>
        <div className="text-center py-12 text-farm-500">
          <VideoOff size={40} className="mx-auto mb-4 text-farm-600" />
          <p>Printer not found</p>
        </div>
      </div>
    )
  }

  const online = printer.last_seen && (Date.now() - new Date(printer.last_seen + 'Z').getTime()) < 90000

  return (
    <div className="p-4 md:p-6">
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-3">
          <Link to="/cameras" className="flex items-center gap-1.5 text-farm-400 hover:text-white transition-colors text-sm">
            <ArrowLeft size={16} /> Cameras
          </Link>
          <span className="text-farm-600">/</span>
          <h1 className="text-xl font-display font-bold truncate">{printer.nickname || printer.name}</h1>
        </div>
        <div className="flex items-center gap-2">
          <AiIndicator printerId={printerId} />
          <div className="flex items-center gap-1.5">
            <div className={`w-2 h-2 rounded-full ${online ? 'bg-green-500' : 'bg-farm-600'}`} />
            <span className={`text-xs ${online ? 'text-green-400' : 'text-farm-500'}`}>{online ? 'Online' : 'Offline'}</span>
          </div>
          <button
            onClick={toggleFullscreen}
            className="p-1.5 bg-farm-800 hover:bg-farm-700 rounded-lg transition-colors text-farm-400 hover:text-white"
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
          <WebRTCPlayer cameraId={printerId} className="aspect-video rounded-lg overflow-hidden" />
        </div>

        {/* Data panels */}
        <div className="flex-[3] min-w-0 space-y-4">
          <PrinterInfoPanel printer={printer} />
          <FilamentSlotsPanel printer={printer} />
          <ActiveJobPanel printer={printer} />
        </div>
      </div>
    </div>
  )
}
