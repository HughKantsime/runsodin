import { useState, useEffect, useRef } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Video, VideoOff, Maximize2, Minimize2, Monitor, Clock, Settings, Power, PictureInPicture2, X, GripVertical, Eye, Tv, RefreshCw } from 'lucide-react'
import clsx from 'clsx'
import CameraModal from '../../components/printers/CameraModal'
import { PageHeader, Button, Card, EmptyState } from '../../components/ui'
import useWebRTC from '../../hooks/useWebRTC'

const API_BASE = '/api'


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
      const clientX = e.touches ? e.touches[0].clientX : e.clientX
      const clientY = e.touches ? e.touches[0].clientY : e.clientY
      setPosition({
        x: Math.max(0, Math.min(window.innerWidth - width, clientX - dragOffset.x)),
        y: Math.max(0, Math.min(window.innerHeight - height, clientY - dragOffset.y)),
      })
    }
    const onUp = () => setDragging(false)
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    window.addEventListener('touchmove', onMove, { passive: false })
    window.addEventListener('touchend', onUp)
    return () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      window.removeEventListener('touchmove', onMove)
      window.removeEventListener('touchend', onUp)
    }
  }, [dragging, dragOffset, width, height])

  const streamUrl = camera.stream_url || `/api/cameras/${camera.id}/stream`

  return (
    <div 
      className="fixed z-[9999] rounded-md overflow-hidden shadow-2xl border border-[var(--brand-card-border)] bg-black"
      style={{ left: position.x, top: position.y, width, height }}
    >
      {/* Header bar */}
      <div 
        className="absolute top-0 left-0 right-0 h-6 bg-[var(--brand-card-bg)]/90 backdrop-blur-sm z-10 flex items-center justify-between px-2 cursor-move"
        onMouseDown={(e) => {
          setDragging(true)
          setDragOffset({ x: e.clientX - position.x, y: e.clientY - position.y })
          e.preventDefault()
        }}
        onTouchStart={(e) => {
          const touch = e.touches[0]
          setDragging(true)
          setDragOffset({ x: touch.clientX - position.x, y: touch.clientY - position.y })
        }}
      >
        <div className="flex items-center gap-1.5">
          <GripVertical size={10} className="text-white/50" />
          <span className="text-[10px] text-white/70 font-medium truncate">{camera.printer_name || camera.name || 'Camera'}</span>
        </div>
        <div className="flex items-center gap-1">
          <button 
            onClick={() => setSize(s => s === 'small' ? 'medium' : 'small')}
            className="p-0.5 hover:bg-white/20 rounded-lg text-white/60 hover:text-white"
          >
            {size === 'small' ? <Maximize2 size={8} /> : <Minimize2 size={8} />}
          </button>
          <button
            onClick={onClose}
            className="p-0.5 hover:bg-red-500/50 rounded-lg text-white/60 hover:text-white"
          >
            <X size={8} />
          </button>
        </div>
      </div>
      {/* Stream */}
      <img
        src={streamUrl}
        alt="Camera stream"
        className="w-full h-full object-cover"
        style={{ pointerEvents: 'none' }}
      />
      {/* Live indicator */}
      <div className="absolute bottom-1.5 left-2 flex items-center gap-1">
        <div className="w-1.5 h-1.5 rounded-full bg-green-500" />
        <span className="text-[10px] font-mono text-white/70">LIVE</span>
      </div>
    </div>
  )
}

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
      <div className="w-1.5 h-1.5 rounded-full bg-purple-400 animate-pulse" />
      AI
    </div>
  )
}

function CameraCard({ camera, onExpand, onPip }) {
  const { videoRef, status, retry: handleRetry } = useWebRTC(camera.id)

  const dotColor = status === 'live' ? 'bg-green-500' : status === 'connecting' ? 'bg-yellow-500 animate-pulse' : 'bg-red-500'

  return (
    <div className="bg-[var(--brand-card-bg)] rounded-md overflow-hidden group" style={{ boxShadow: 'var(--brand-card-shadow)' }}>
      <div className="relative aspect-video bg-black">
        <video ref={videoRef} autoPlay playsInline muted className="w-full h-full object-contain" />
        {status === 'connecting' && (
          <div className="absolute inset-0 flex items-center justify-center">
            <div className="text-[var(--brand-text-muted)] text-sm animate-pulse">Connecting...</div>
          </div>
        )}
        {(status === 'error' || status === 'disconnected') && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2">
            <VideoOff size={32} className="text-[var(--brand-text-muted)]" />
            <button
              onClick={handleRetry}
              className="flex items-center gap-1.5 px-3 py-1 bg-[var(--brand-surface)] hover:bg-[var(--brand-surface)] rounded-lg text-xs text-[var(--brand-text-secondary)] transition-colors"
            >
              <RefreshCw size={12} /> Retry
            </button>
          </div>
        )}
        {/* Status overlay - bottom left */}
        {status === 'live' && (
          <div className="absolute bottom-2 left-2 flex items-center gap-1">
            <div className="w-1.5 h-1.5 rounded-full bg-green-500" />
            <span className="text-[10px] font-mono text-white/70">LIVE</span>
          </div>
        )}
        <div className="absolute top-2 right-2 flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
          <button
            onClick={() => onPip(camera)}
            className="p-1.5 backdrop-blur-sm bg-black/40 rounded-md hover:bg-black/80"
            title="Picture-in-Picture"
          >
            <PictureInPicture2 size={16} />
          </button>
          <Link to={`/cameras/${camera.id}`} className="p-1.5 backdrop-blur-sm bg-black/40 rounded-md hover:bg-black/80">
            <Maximize2 size={16} />
          </Link>
        </div>
      </div>
      <div className="px-2 py-1.5 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <div className={'w-2 h-2 rounded-full ' + dotColor} />
          <Link to={`/cameras/${camera.id}`} className="font-medium text-sm hover:text-[var(--brand-primary)] transition-colors">{camera.name}</Link>
          <AiIndicator printerId={camera.id} />
        </div>
        <span className="text-xs text-[var(--brand-text-muted)] capitalize">{status}</span>
      </div>
    </div>
  )
}

export default function Cameras() {
  const queryClient = useQueryClient()
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

  // Keyboard shortcut: Shift+F for fullscreen control room
  useEffect(() => {
    const handleKey = (e) => {
      if (e.key === 'F' && e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey) {
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

  // Hide sidebar and header in control room mode via CSS class on root element
  useEffect(() => {
    if (controlRoom) {
      document.documentElement.classList.add('control-room-mode')
    } else {
      document.documentElement.classList.remove('control-room-mode')
    }
    return () => document.documentElement.classList.remove('control-room-mode')
  }, [controlRoom])

  const [filter, setFilter] = useState('')
  const [showSettings, setShowSettings] = useState(false)
  const [allPrinters, setAllPrinters] = useState([])

  // Fetch all printers (including disabled cameras) for settings panel
  useEffect(() => {
    const fetchPrinters = async () => {
      try {
        const res = await fetch(API_BASE + '/printers', { credentials: 'include' })
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
      const res = await fetch(API_BASE + '/cameras/' + printerId + '/toggle', {
        method: 'PATCH',
        credentials: 'include',
      })
      if (res.ok) {
        const data = await res.json()
        setAllPrinters(prev => prev.map(p =>
          p.id === printerId ? { ...p, camera_enabled: data.camera_enabled } : p
        ))
        queryClient.invalidateQueries({ queryKey: ['cameras'] })
      }
    } catch (err) {
      console.error('Failed to toggle camera:', err)
    }
  }

  const { data: cameras, isLoading } = useQuery({
    queryKey: ['cameras'],
    queryFn: async () => {
      const response = await fetch(API_BASE + '/cameras', { credentials: 'include' })
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
      <PageHeader
        icon={Video}
        title="Cameras"
        subtitle={`${filteredCameras.length} camera${filteredCameras.length !== 1 ? 's' : ''} available`}
      >
        <input
          type="text"
          placeholder="Filter cameras..."
          value={filter}
          onChange={e => setFilter(e.target.value)}
          className="bg-transparent border-b border-[var(--brand-card-border)] focus:border-[var(--brand-primary)] rounded-none px-2 py-1.5 text-sm w-36 md:w-40 placeholder-[var(--brand-text-muted)] focus:outline-none"
        />
        <div className="flex rounded-md border border-[var(--brand-card-border)] overflow-hidden">
          {[1, 2, 3].map(n => (
            <button
              key={n}
              onClick={() => setColumns(n)}
              className={clsx(
                'px-2.5 py-1 text-xs font-mono transition-colors',
                columns === n
                  ? 'bg-[var(--brand-primary)] text-white'
                  : 'text-[var(--brand-text-secondary)] hover:bg-[var(--brand-surface)]'
              )}
            >
              {n === 1 ? '1\u00d71' : n === 2 ? '2\u00d72' : '3\u00d73'}
            </button>
          ))}
        </div>
        <Button
          variant={showSettings ? 'tertiary' : 'ghost'}
          size="icon"
          icon={Settings}
          onClick={() => setShowSettings(!showSettings)}
          title="Camera Settings"
        />
        <Button
          icon={Monitor}
          onClick={() => setControlRoom(true)}
          title="Control Room Mode (Shift+F)"
        >
          <span className="hidden sm:inline">Control Room</span>
        </Button>
        <a
          href="/tv"
          target="_blank"
          rel="noopener noreferrer"
          className="flex items-center gap-1.5 px-3 py-1.5 bg-[var(--brand-surface)] hover:bg-[var(--brand-surface)] rounded-lg text-sm font-medium transition-colors text-[var(--brand-text-secondary)]"
          title="Open TV Dashboard in new tab"
        >
          <Tv size={14} />
          <span className="hidden sm:inline">TV Mode</span>
        </a>
      </PageHeader>

      {/* Camera Settings Panel */}
      {showSettings && (
        <Card className="mb-4">
          <div className="flex items-center justify-between mb-3">
            <h3 className="font-medium">Camera Settings</h3>
            <Button variant="ghost" size="sm" onClick={() => setShowSettings(false)}>
              Close
            </Button>
          </div>
          <div className="space-y-2">
            {allPrinters.length === 0 && (
              <p className="text-[var(--brand-text-muted)] text-sm">No printers with cameras configured</p>
            )}
            {allPrinters.map(printer => (
              <div key={printer.id} className="flex items-center justify-between py-2 px-3 bg-[var(--brand-card-bg)] rounded-lg">
                <div className="flex items-center gap-3">
                  <Video size={16} className="text-[var(--brand-text-muted)]" />
                  <span className="text-sm">{printer.nickname || printer.name}</span>
                </div>
                <Button
                  variant={printer.camera_enabled !== false ? 'success' : 'secondary'}
                  size="sm"
                  icon={Power}
                  onClick={() => toggleCamera(printer.id)}
                >
                  {printer.camera_enabled !== false ? 'Enabled' : 'Disabled'}
                </Button>
              </div>
            ))}
          </div>
          <p className="text-xs text-[var(--brand-text-muted)] mt-3">
            Disabled cameras won't appear in the grid or Control Room.
          </p>
        </Card>
      )}

      {isLoading && (
        <div className="text-center text-[var(--brand-text-muted)] py-12 text-sm">Loading cameras...</div>
      )}

      {filteredCameras.length === 0 && !isLoading && (
        <EmptyState
          icon={VideoOff}
          title="No cameras configured"
          description="Add camera URLs in printer settings"
        />
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
          <div className="absolute top-0 left-0 right-0 z-10 flex items-center justify-between p-3 bg-gradient-to-b from-black/80 to-transparent">
            <div className="flex items-center gap-3 text-white">
              <Monitor size={16} />
              <span className="text-sm font-semibold">Control Room</span>
              <span className="text-xs text-[var(--brand-text-muted)]">
                {filteredCameras.length} camera{filteredCameras.length !== 1 ? 's' : ''}
              </span>
            </div>
            <div className="flex items-center gap-4">
              <div className="flex items-center gap-2 text-white font-mono text-base">
                <Clock size={14} />
                {currentTime.toLocaleTimeString()}
              </div>
              <button onClick={() => setControlRoom(false)} className="px-3 py-1 text-xs text-[var(--brand-text-secondary)] hover:text-white transition-colors">Exit (Esc)</button>
            </div>
          </div>

          {/* Camera Grid */}
          <div className="absolute inset-0 pt-16 pb-4 px-4 overflow-hidden">
            <div className={`grid gap-px h-full ${
              filteredCameras.length === 1 ? 'grid-cols-1' :
              filteredCameras.length === 2 ? 'grid-cols-2' :
              filteredCameras.length <= 4 ? 'grid-cols-2' :
              filteredCameras.length <= 6 ? 'grid-cols-3' :
              filteredCameras.length <= 9 ? 'grid-cols-3' :
              'grid-cols-4'
            }`}>
              {filteredCameras.map((camera, index) => (
                <div key={camera.id} className="relative bg-black overflow-hidden">
                  <ControlRoomCamera camera={camera} delay={index * 500} />
                  <div className="absolute bottom-0 left-0 right-0 p-2 bg-gradient-to-t from-black/80 to-transparent">
                    <span className="text-[10px] font-mono text-white">{camera.name}</span>
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
function ControlRoomCamera({ camera, delay = 0 }) {
  const [ready, setReady] = useState(false)

  // Stagger connections so cameras don't all hit go2rtc at once
  useEffect(() => {
    if (delay === 0) { setReady(true); return }
    const timer = setTimeout(() => setReady(true), delay)
    return () => clearTimeout(timer)
  }, [delay])

  const { videoRef, status, retry } = useWebRTC(camera.id, {
    maxRetries: 0, // infinite retries for control room
    enabled: ready,
  })

  return (
    <>
      <video ref={videoRef} autoPlay playsInline muted className="w-full h-full object-contain" />
      {(!ready || status === 'connecting') && (
        <div className="absolute inset-0 flex items-center justify-center">
          <div className="text-[var(--brand-text-muted)] text-sm animate-pulse">Connecting...</div>
        </div>
      )}
      {(status === 'error' || status === 'disconnected') && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-2">
          <VideoOff size={32} className="text-[var(--brand-text-muted)]" />
          <span className="text-xs text-[var(--brand-text-muted)]">Reconnecting...</span>
        </div>
      )}
      {status === 'live' && (
        <div className="absolute top-2 right-2">
          <div className="w-1.5 h-1.5 bg-green-500 rounded-full" />
        </div>
      )}
    </>
  )
}
