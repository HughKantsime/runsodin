import { useState, useEffect, useRef } from 'react'
import { useParams, useNavigate, Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowLeft, Maximize2, Minimize2, VideoOff, Eye, Thermometer, Wind, Activity, RefreshCw } from 'lucide-react'
import { printers } from '../api'

const API_BASE = '/api'
const API_KEY = import.meta.env.VITE_API_KEY

function AiIndicator({ printerId }) {
  const { data } = useQuery({
    queryKey: ['vision-settings', printerId],
    queryFn: async () => {
      const token = localStorage.getItem('token')
      const headers = { 'Content-Type': 'application/json', 'X-API-Key': API_KEY }
      if (token) headers['Authorization'] = 'Bearer ' + token
      const res = await fetch(`${API_BASE}/printers/${printerId}/vision`, { headers })
      if (!res.ok) return null
      return res.json()
    },
    refetchInterval: 60000,
    retry: false,
  })
  if (!data || !data.enabled) return null
  return (
    <div className="flex items-center gap-1 px-1.5 py-0.5 bg-purple-900/50 rounded-lg text-[10px] text-purple-300 font-medium" title="Vision AI active">
      <Eye size={10} />
      <div className="w-1.5 h-1.5 rounded-full bg-purple-400 animate-pulse" />
      AI
    </div>
  )
}

function WebRTCPlayer({ cameraId, className }) {
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
      const token = localStorage.getItem('token')
      const headers = { 'Content-Type': 'application/sdp', 'X-API-Key': API_KEY }
      if (token) headers['Authorization'] = 'Bearer ' + token
      const response = await fetch(`${API_BASE}/cameras/${cameraId}/webrtc`, { method: 'POST', headers, body: offer.sdp })
      if (!response.ok) throw new Error('Failed')
      const answerSDP = await response.text()
      await pc.setRemoteDescription(new RTCSessionDescription({ type: 'answer', sdp: answerSDP }))
    } catch {
      setStatus('error')
    }
  }

  return (
    <div className={`relative bg-black ${className || ''}`}>
      <video ref={videoRef} autoPlay playsInline muted className="w-full h-full object-contain" />
      {status === 'connecting' && (
        <div className="absolute inset-0 flex items-center justify-center">
          <div className="text-farm-500 text-sm animate-pulse">Connecting...</div>
        </div>
      )}
      {status === 'error' && (
        <div className="absolute inset-0 flex items-center justify-center">
          <VideoOff size={48} className="text-farm-600" />
        </div>
      )}
      {status === 'live' && (
        <div className="absolute top-3 left-3 flex items-center gap-1.5">
          <div className="w-2 h-2 bg-green-500 rounded-full animate-pulse" />
          <span className="text-xs text-white/70 font-medium">LIVE</span>
        </div>
      )}
    </div>
  )
}

function PrinterInfoPanel({ printer }) {
  const online = printer.last_seen && (Date.now() - new Date(printer.last_seen + 'Z').getTime()) < 90000
  const nozTemp = printer.nozzle_temp != null ? Math.round(printer.nozzle_temp) : null
  const nozTarget = printer.nozzle_target_temp != null ? Math.round(printer.nozzle_target_temp) : null
  const bedTemp = printer.bed_temp != null ? Math.round(printer.bed_temp) : null
  const bedTarget = printer.bed_target_temp != null ? Math.round(printer.bed_target_temp) : null
  const fanSpeed = printer.fan_speed != null ? Math.round(printer.fan_speed) : null
  const stage = printer.print_stage && printer.print_stage !== 'Idle' ? printer.print_stage : null

  return (
    <div className="bg-farm-900 rounded-lg border border-farm-800 p-4">
      <h3 className="text-sm font-medium text-farm-400 mb-3">Printer Status</h3>
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <span className="text-sm text-farm-400">Status</span>
          <div className="flex items-center gap-2">
            <div className={`w-2 h-2 rounded-full ${online ? 'bg-green-500' : 'bg-farm-600'}`} />
            <span className={`text-sm ${online ? 'text-green-400' : 'text-farm-500'}`}>{online ? 'Online' : 'Offline'}</span>
          </div>
        </div>
        {stage && (
          <div className="flex items-center justify-between">
            <span className="text-sm text-farm-400">Stage</span>
            <span className="text-sm text-print-400">{stage}</span>
          </div>
        )}
        {nozTemp != null && (
          <div className="flex items-center justify-between">
            <span className="text-sm text-farm-400 flex items-center gap-1.5"><Thermometer size={13} /> Nozzle</span>
            <span className={`text-sm ${nozTarget > 0 ? 'text-orange-400' : 'text-farm-300'}`}>
              {nozTemp}°{nozTarget > 0 ? `/${nozTarget}°C` : 'C'}
            </span>
          </div>
        )}
        {bedTemp != null && (
          <div className="flex items-center justify-between">
            <span className="text-sm text-farm-400 flex items-center gap-1.5"><Thermometer size={13} /> Bed</span>
            <span className={`text-sm ${bedTarget > 0 ? 'text-orange-400' : 'text-farm-300'}`}>
              {bedTemp}°{bedTarget > 0 ? `/${bedTarget}°C` : 'C'}
            </span>
          </div>
        )}
        {fanSpeed != null && (
          <div className="flex items-center justify-between">
            <span className="text-sm text-farm-400 flex items-center gap-1.5"><Wind size={13} /> Fan</span>
            <span className="text-sm text-farm-300">{fanSpeed}%</span>
          </div>
        )}
      </div>
    </div>
  )
}

function FilamentSlotsPanel({ printer }) {
  const slots = printer.filament_slots || []
  if (!slots.length) return null

  const getShortName = (slot) => {
    const color = slot?.color
    if (!color || color.startsWith('#') || /^[0-9a-fA-F]{6}$/.test(color)) return slot?.filament_type || 'Empty'
    const brands = ['Bambu Lab', 'Polymaker', 'Hatchbox', 'eSun', 'Prusament', 'Overture', 'Generic']
    let short = color
    for (const brand of brands) {
      if (color.startsWith(brand + ' ')) { short = color.slice(brand.length + 1); break }
    }
    if (short.length > 14) return short.slice(0, 12) + '...'
    return short
  }

  return (
    <div className="bg-farm-900 rounded-lg border border-farm-800 p-4">
      <h3 className="text-sm font-medium text-farm-400 mb-3">Filament</h3>
      <div className="space-y-2">
        {slots.map((slot, idx) => (
          <div key={idx} className="flex items-center gap-3">
            <div
              className="w-4 h-4 rounded-lg flex-shrink-0"
              style={{ backgroundColor: slot.color_hex ? `#${slot.color_hex}` : (slot.color ? '#888' : '#333') }}
            />
            <span className="text-sm text-farm-300 flex-1 truncate">{getShortName(slot)}</span>
            {slot.remaining_weight != null && (
              <span className={`text-xs ${slot.remaining_weight < 100 ? 'text-amber-400' : 'text-farm-500'}`}>
                {Math.round(slot.remaining_weight)}g
              </span>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

function ActiveJobPanel({ printer }) {
  const isPrinting = printer.gcode_state === 'RUNNING' || printer.gcode_state === 'PAUSE'
  if (!isPrinting) return null

  const progress = printer.mc_percent || 0
  const remaining = printer.mc_remaining_time
  const jobName = printer.gcode_file || 'Printing'
  const currentLayer = printer.layer_num
  const totalLayers = printer.total_layer_num

  const formatTime = (minutes) => {
    if (!minutes) return ''
    if (minutes < 60) return `${Math.round(minutes)}m left`
    return `${Math.floor(minutes / 60)}h ${Math.round(minutes % 60)}m left`
  }

  return (
    <div className="bg-farm-900 rounded-lg border border-farm-800 p-4">
      <h3 className="text-sm font-medium text-farm-400 mb-3 flex items-center gap-1.5">
        <Activity size={13} className="text-print-400 animate-pulse" /> Current Job
      </h3>
      <p className="text-sm font-medium mb-3 truncate">{jobName}</p>
      <div className="flex items-center justify-between mb-2">
        <span className="text-2xl font-bold text-green-400">{progress}%</span>
        {remaining && <span className="text-xs text-farm-500">{formatTime(remaining)}</span>}
      </div>
      <div className="w-full bg-farm-700 rounded-full h-2.5 mb-2">
        <div className="bg-green-500 h-2.5 rounded-full transition-all duration-500" style={{ width: `${progress}%` }} />
      </div>
      {currentLayer && totalLayers && (
        <p className="text-xs text-farm-500">Layer {currentLayer}/{totalLayers}</p>
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
