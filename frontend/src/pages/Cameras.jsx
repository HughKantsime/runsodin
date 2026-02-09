import { useState, useEffect, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Video, VideoOff, Maximize2, Minimize2, Rows3, LayoutGrid, Columns3, Monitor, Clock, Settings, Power, PictureInPicture2, X, Move } from 'lucide-react'
import CameraModal from '../components/CameraModal'

const API_BASE = '/api'
const API_KEY = import.meta.env.VITE_API_KEY


function PipPlayer({ camera, onClose }) {
  const [position, setPosition] = useState({ x: window.innerWidth - 340, y: window.innerHeight - 240 })
  const [dragging, setDragging] = useState(false)
  const [dragOffset, setDragOffset] = useState({ x: 0, y: 0 })
  const [size, setSize] = useState('medium') // small, medium
  
  const sizes = {
    small: { width: 240, height: 180 },
    medium: { width: 320, height: 220 },
  }
  const { width, height } = sizes[size]

  useEffect(() => {
    if (!dragging) return
    const onMove = (e) => {
      setPosition({
        x: Math.max(0, Math.min(window.innerWidth - width, e.clientX - dragOffset.x)),
        y: Math.max(0, Math.min(window.innerHeight - height, e.clientY - dragOffset.y)),
      })
    }
    const onUp = () => setDragging(false)
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    return () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
  }, [dragging, dragOffset, width, height])

  const streamUrl = camera.stream_url || `/api/cameras/${camera.id}/stream`

  return (
    <div 
      className="fixed z-[9999] rounded-xl overflow-hidden shadow-2xl border border-farm-700 bg-black"
      style={{ left: position.x, top: position.y, width, height }}
    >
      {/* Header bar */}
      <div 
        className="absolute top-0 left-0 right-0 h-7 bg-gradient-to-b from-black/80 to-transparent z-10 flex items-center justify-between px-2 cursor-move"
        onMouseDown={(e) => {
          setDragging(true)
          setDragOffset({ x: e.clientX - position.x, y: e.clientY - position.y })
          e.preventDefault()
        }}
      >
        <div className="flex items-center gap-1.5">
          <Move size={10} className="text-white/50" />
          <span className="text-[10px] text-white/70 font-medium truncate">{camera.printer_name || camera.name || 'Camera'}</span>
        </div>
        <div className="flex items-center gap-1">
          <button 
            onClick={() => setSize(s => s === 'small' ? 'medium' : 'small')}
            className="p-0.5 hover:bg-white/20 rounded text-white/60 hover:text-white"
          >
            {size === 'small' ? <Maximize2 size={10} /> : <Minimize2 size={10} />}
          </button>
          <button 
            onClick={onClose}
            className="p-0.5 hover:bg-red-500/50 rounded text-white/60 hover:text-white"
          >
            <X size={10} />
          </button>
        </div>
      </div>
      {/* Stream */}
      <img 
        src={streamUrl}
        alt=""
        className="w-full h-full object-cover"
        style={{ pointerEvents: 'none' }}
      />
      {/* Live indicator */}
      <div className="absolute bottom-1.5 left-2 flex items-center gap-1">
        <div className="w-1.5 h-1.5 rounded-full bg-red-500 animate-pulse" />
        <span className="text-[9px] text-white/70 font-medium">LIVE</span>
      </div>
    </div>
  )
}

function CameraCard({ camera, onExpand, onPip }) {
  const videoRef = useRef(null)
  const pcRef = useRef(null)
  const [status, setStatus] = useState('connecting')

  useEffect(() => {
    startWebRTC()
    return () => {
      if (pcRef.current) { pcRef.current.close(); pcRef.current = null }
    }
  }, [camera.id])

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

      const response = await fetch(API_BASE + '/cameras/' + camera.id + '/webrtc', { method: 'POST', headers, body: offer.sdp })
      if (!response.ok) throw new Error('Failed')

      const answerSDP = await response.text()
      await pc.setRemoteDescription(new RTCSessionDescription({ type: 'answer', sdp: answerSDP }))
    } catch (err) {
      setStatus('error')
    }
  }

  const dotColor = status === 'live' ? 'bg-green-500' : status === 'connecting' ? 'bg-yellow-500 animate-pulse' : 'bg-red-500'

  return (
    <div className="bg-farm-950 rounded border border-farm-800 overflow-hidden group">
      <div className="relative aspect-video bg-black">
        <video ref={videoRef} autoPlay playsInline muted className="w-full h-full object-contain" />
        {status === 'connecting' && (
          <div className="absolute inset-0 flex items-center justify-center">
            <div className="text-farm-500 text-sm animate-pulse">Connecting...</div>
          </div>
        )}
        {status === 'error' && (
          <div className="absolute inset-0 flex items-center justify-center">
            <VideoOff size={32} className="text-farm-600" />
          </div>
        )}
        <button onClick={() => onExpand(camera)} className="absolute top-2 right-2 p-1.5 bg-black/50 rounded-lg opacity-0 group-hover:opacity-100 transition-opacity hover:bg-black/80">
          <Maximize2 size={16} />
        </button>
      </div>
      <div className="p-2 md:p-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <div className={'w-2 h-2 rounded-full ' + dotColor} />
          <span className="font-medium text-sm">{camera.name}</span>
        </div>
        <span className="text-xs text-farm-500 capitalize">{status}</span>
      </div>
    </div>
  )
}

export default function Cameras() {
  const [expandedCamera, setExpandedCamera] = useState(null)
  const [columns, setColumns] = useState(2)
  const [controlRoom, setControlRoom] = useState(false)
  const [pipCamera, setPipCamera] = useState(null)
  const [currentTime, setCurrentTime] = useState(new Date())

  // Update clock every second in control room mode
  useEffect(() => {
    if (!controlRoom) return
    const interval = setInterval(() => setCurrentTime(new Date()), 1000)
    return () => clearInterval(interval)
  }, [controlRoom])

  // Keyboard shortcut: F for fullscreen control room
  useEffect(() => {
    const handleKey = (e) => {
      if (e.key === 'f' && !e.ctrlKey && !e.metaKey && !e.altKey) {
        // Don't trigger if typing in input
        if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return
        setControlRoom(prev => !prev)
      }
      if (e.key === 'Escape' && controlRoom) {
        setControlRoom(false)
      }
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [controlRoom])

  // Hide sidebar and header in control room mode
  useEffect(() => {
    if (controlRoom) {
      document.body.classList.add('control-room-mode')
      // Try to hide sidebar
      const sidebar = document.querySelector('[class*="sidebar"], [class*="Sidebar"], nav')
      const header = document.querySelector('header, [class*="header"], [class*="Header"]')
      if (sidebar) sidebar.style.display = 'none'
      if (header) header.style.display = 'none'
    } else {
      document.body.classList.remove('control-room-mode')
      // Restore sidebar and header
      const sidebar = document.querySelector('[class*="sidebar"], [class*="Sidebar"], nav')
      const header = document.querySelector('header, [class*="header"], [class*="Header"]')
      if (sidebar) sidebar.style.display = ''
      if (header) header.style.display = ''
    }
    return () => {
      document.body.classList.remove('control-room-mode')
      const sidebar = document.querySelector('[class*="sidebar"], [class*="Sidebar"], nav')
      const header = document.querySelector('header, [class*="header"], [class*="Header"]')
      if (sidebar) sidebar.style.display = ''
      if (header) header.style.display = ''
    }
  }, [controlRoom])

  const [filter, setFilter] = useState('')
  const [showSettings, setShowSettings] = useState(false)
  const [allPrinters, setAllPrinters] = useState([])

  // Fetch all printers (including disabled cameras) for settings panel
  useEffect(() => {
    const fetchPrinters = async () => {
      try {
        const token = localStorage.getItem('token')
        const headers = { 'X-API-Key': API_KEY }
        if (token) headers['Authorization'] = 'Bearer ' + token
        const res = await fetch(API_BASE + '/printers', { headers })
        if (res.ok) {
          const data = await res.json()
          setAllPrinters(data.filter(p => p.camera_url))
        }
      } catch (err) {
        console.error('Failed to fetch printers:', err)
      }
    }
    if (showSettings) fetchPrinters()
  }, [showSettings])

  const toggleCamera = async (printerId) => {
    try {
      const token = localStorage.getItem('token')
      const headers = { 'X-API-Key': API_KEY }
      if (token) headers['Authorization'] = 'Bearer ' + token
      const res = await fetch(API_BASE + '/cameras/' + printerId + '/toggle', {
        method: 'PATCH',
        headers
      })
      if (res.ok) {
        const data = await res.json()
        setAllPrinters(prev => prev.map(p => 
          p.id === printerId ? { ...p, camera_enabled: data.camera_enabled } : p
        ))
      }
    } catch (err) {
      console.error('Failed to toggle camera:', err)
    }
  }

  const { data: cameras, isLoading } = useQuery({
    queryKey: ['cameras'],
    queryFn: async () => {
      const token = localStorage.getItem('token')
      const headers = { 'X-API-Key': API_KEY }
      if (token) headers['Authorization'] = 'Bearer ' + token
      const response = await fetch(API_BASE + '/cameras', { headers })
      if (!response.ok) throw new Error('Failed to fetch cameras')
      return response.json()
    },
    refetchInterval: 30000
  })

  const filteredCameras = (cameras || []).filter(cam =>
    !filter || cam.name.toLowerCase().includes(filter.toLowerCase())
  )

  // Auto-calculate optimal columns based on camera count
  const autoColumns = Math.min(Math.ceil(Math.sqrt(filteredCameras?.length || 1)), 4)

  const gridClass = columns === 1
    ? 'grid-cols-1'
    : columns === 2
      ? 'grid-cols-1 md:grid-cols-2'
      : 'grid-cols-1 sm:grid-cols-2 lg:grid-cols-3'

  return (
    <div className="p-4 md:p-6">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-4 md:mb-6">
        <div>
          <h2 className="text-xl md:text-2xl font-display font-bold">Cameras</h2>
          <p className="text-xs md:text-sm text-farm-500 mt-1">
            {filteredCameras.length} camera{filteredCameras.length !== 1 ? 's' : ''} available
          </p>
        </div>
        <div className="flex items-center gap-2 md:gap-3">
          <input
            type="text"
            placeholder="Filter cameras..."
            value={filter}
            onChange={e => setFilter(e.target.value)}
            className="bg-farm-900 border border-farm-700 rounded-lg px-3 py-1.5 text-sm w-36 md:w-40 placeholder-farm-600 focus:outline-none focus:border-farm-500"
          />
          <div className="flex items-center gap-1 bg-farm-900 rounded-lg p-1">
            <button onClick={() => setColumns(1)} className={'p-1.5 rounded ' + (columns === 1 ? 'bg-farm-700 text-white' : 'text-farm-400 hover:text-white')}>
              <Rows3 size={14} />
            </button>
            <button onClick={() => setColumns(2)} className={'p-1.5 rounded ' + (columns === 2 ? 'bg-farm-700 text-white' : 'text-farm-400 hover:text-white')}>
              <LayoutGrid size={14} />
            </button>
            <button onClick={() => setColumns(3)} className={'p-1.5 rounded ' + (columns === 3 ? 'bg-farm-700 text-white' : 'text-farm-400 hover:text-white')}>
              <Columns3 size={14} />
            </button>
          </div>
          <button
            onClick={() => setShowSettings(!showSettings)}
            className={'p-2 rounded-lg transition-colors ' + (showSettings ? 'bg-farm-700 text-white' : 'bg-farm-900 text-farm-400 hover:text-white')}
            title="Camera Settings"
          >
            <Settings size={16} />
          </button>
          <button
            onClick={() => setControlRoom(true)}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-print-600 hover:bg-print-500 rounded-lg text-sm font-medium transition-colors"
            title="Control Room Mode (F)"
          >
            <Monitor size={14} />
            <span className="hidden sm:inline">Control Room</span>
          </button>
        </div>
      </div>

      {/* Camera Settings Panel */}
      {showSettings && (
        <div className="mb-4 p-4 bg-farm-900 rounded border border-farm-800">
          <div className="flex items-center justify-between mb-3">
            <h3 className="font-medium">Camera Settings</h3>
            <button onClick={() => setShowSettings(false)} className="text-farm-400 hover:text-white text-sm">
              Close
            </button>
          </div>
          <div className="space-y-2">
            {allPrinters.length === 0 && (
              <p className="text-farm-500 text-sm">No printers with cameras configured</p>
            )}
            {allPrinters.map(printer => (
              <div key={printer.id} className="flex items-center justify-between py-2 px-3 bg-farm-800 rounded-lg">
                <div className="flex items-center gap-3">
                  <Video size={16} className="text-farm-400" />
                  <span className="text-sm">{printer.nickname || printer.name}</span>
                </div>
                <button
                  onClick={() => toggleCamera(printer.id)}
                  className={'flex items-center gap-2 px-3 py-1 rounded text-sm transition-colors ' + 
                    (printer.camera_enabled !== false
                      ? 'bg-green-900/50 text-green-400 hover:bg-green-900/70'
                      : 'bg-farm-700 text-farm-400 hover:bg-farm-600')}
                >
                  <Power size={14} />
                  {printer.camera_enabled !== false ? 'Enabled' : 'Disabled'}
                </button>
              </div>
            ))}
          </div>
          <p className="text-xs text-farm-500 mt-3">
            Disabled cameras won't appear in the grid or Control Room. Reload page after changes.
          </p>
        </div>
      )}

      {isLoading && (
        <div className="text-center text-farm-500 py-12 text-sm">Loading cameras...</div>
      )}

      {filteredCameras.length === 0 && !isLoading && (
        <div className="text-center text-farm-500 py-12">
          <VideoOff size={40} className="mx-auto mb-4 text-farm-600" />
          <p className="text-sm">No cameras configured</p>
          <p className="text-xs mt-1">Add camera URLs in printer settings</p>
        </div>
      )}

      {filteredCameras.length > 0 && (
        <div className={'grid gap-3 md:gap-4 ' + gridClass}>
          {filteredCameras.map(camera => (
            <CameraCard key={camera.id} camera={camera} onExpand={setExpandedCamera} onPip={setPipCamera} />
          ))}
        </div>
      )}

      {pipCamera && <PipPlayer camera={pipCamera} onClose={() => setPipCamera(null)} />}
      {expandedCamera && (
        <CameraModal printer={expandedCamera} onClose={() => setExpandedCamera(null)} />
      )}

      {/* Control Room Mode Overlay */}
      {controlRoom && (
        <div className="fixed inset-0 z-50 bg-black">
          {/* Top bar with clock and exit button */}
          <div className="absolute top-0 left-0 right-0 z-10 flex items-center justify-between p-4 bg-gradient-to-b from-black/80 to-transparent">
            <div className="flex items-center gap-3 text-white">
              <Monitor size={20} />
              <span className="font-display font-bold text-lg">Control Room</span>
              <span className="text-farm-400 text-sm">
                {filteredCameras.length} camera{filteredCameras.length !== 1 ? 's' : ''}
              </span>
            </div>
            <div className="flex items-center gap-4">
              <div className="flex items-center gap-2 text-white font-mono text-xl">
                <Clock size={18} />
                {currentTime.toLocaleTimeString()}
              </div>
              <button
                onClick={() => setControlRoom(false)}
                className="flex items-center gap-2 px-3 py-1.5 bg-farm-800 hover:bg-farm-700 rounded-lg text-sm transition-colors"
              >
                <Minimize2 size={14} />
                Exit (Esc)
              </button>
            </div>
          </div>

          {/* Camera Grid */}
          <div className="absolute inset-0 pt-16 pb-4 px-4 overflow-hidden">
            <div className={`grid gap-2 h-full ${
              filteredCameras.length === 1 ? 'grid-cols-1' :
              filteredCameras.length === 2 ? 'grid-cols-2' :
              filteredCameras.length <= 4 ? 'grid-cols-2' :
              filteredCameras.length <= 6 ? 'grid-cols-3' :
              filteredCameras.length <= 9 ? 'grid-cols-3' :
              'grid-cols-4'
            }`}>
              {filteredCameras.map(camera => (
                <div key={camera.id} className="relative bg-black rounded-lg overflow-hidden">
                  <ControlRoomCamera camera={camera} />
                  <div className="absolute bottom-0 left-0 right-0 p-2 bg-gradient-to-t from-black/80 to-transparent">
                    <span className="text-white text-sm font-medium">{camera.name}</span>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// Simplified camera component for control room (no controls, just video)
function ControlRoomCamera({ camera }) {
  const videoRef = useRef(null)
  const pcRef = useRef(null)
  const [status, setStatus] = useState('connecting')

  useEffect(() => {
    startWebRTC()
    return () => {
      if (pcRef.current) { pcRef.current.close(); pcRef.current = null }
    }
  }, [camera.id])

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
      const response = await fetch(API_BASE + '/cameras/' + camera.id + '/webrtc', { method: 'POST', headers, body: offer.sdp })
      if (!response.ok) throw new Error('Failed')
      const answerSDP = await response.text()
      await pc.setRemoteDescription(new RTCSessionDescription({ type: 'answer', sdp: answerSDP }))
    } catch (err) {
      setStatus('error')
    }
  }

  return (
    <>
      <video ref={videoRef} autoPlay playsInline muted className="w-full h-full object-contain" />
      {status === 'connecting' && (
        <div className="absolute inset-0 flex items-center justify-center">
          <div className="text-farm-500 text-sm animate-pulse">Connecting...</div>
        </div>
      )}
      {status === 'error' && (
        <div className="absolute inset-0 flex items-center justify-center">
          <VideoOff size={32} className="text-farm-600" />
        </div>
      )}
      {status === 'live' && (
        <div className="absolute top-2 right-2">
          <div className="w-2 h-2 bg-green-500 rounded-full animate-pulse" />
        </div>
      )}
    </>
  )
}
