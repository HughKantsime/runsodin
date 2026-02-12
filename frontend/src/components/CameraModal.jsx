import { useState, useEffect, useRef } from 'react'
import { X, Maximize2, Minimize2, Monitor } from 'lucide-react'

export default function CameraModal({ printer, onClose }) {
  const videoRef = useRef(null)
  const pcRef = useRef(null)
  const [status, setStatus] = useState('connecting')
  const [error, setError] = useState(null)
  const [size, setSize] = useState('small') // small, large, fullscreen

  useEffect(() => {
    if (!printer) return
    startWebRTC()
    return () => {
      if (pcRef.current) {
        pcRef.current.close()
        pcRef.current = null
      }
    }
  }, [printer])

  useEffect(() => {
    const handleEsc = (e) => {
      if (e.key === 'Escape') {
        if (size === 'fullscreen') setSize('large')
        else onClose()
      }
    }
    window.addEventListener('keydown', handleEsc)
    return () => window.removeEventListener('keydown', handleEsc)
  }, [size, onClose])

  const startWebRTC = async () => {
    try {
      setStatus('connecting')
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

      const token = localStorage.getItem('token')
      const headers = {
        'Content-Type': 'application/sdp',
        'X-API-Key': import.meta.env.VITE_API_KEY,
      }
      if (token) headers['Authorization'] = 'Bearer ' + token

      const response = await fetch('/api/cameras/' + printer.id + '/webrtc', {
        method: 'POST',
        headers: headers,
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
  }

  if (!printer) return null

  const dotColor = status === 'live'
    ? 'bg-green-500'
    : status === 'connecting'
      ? 'bg-yellow-500 animate-pulse'
      : 'bg-red-500'

  const sizeClasses = size === 'fullscreen'
    ? 'fixed inset-0 z-[60] bg-black'
    : size === 'large'
      ? 'fixed inset-0 z-50 bg-black/80 flex items-center justify-center p-4'
      : 'fixed bottom-4 right-4 z-50'

  const containerClasses = size === 'fullscreen'
    ? 'w-full h-full flex flex-col'
    : size === 'large'
      ? 'bg-farm-950 rounded-lg border border-farm-800 w-full max-w-5xl flex flex-col'
      : 'bg-farm-950 rounded-lg border border-farm-800 shadow-2xl w-96 flex flex-col'

  return (
    <div className={sizeClasses} role="dialog" aria-modal={size !== 'small' ? 'true' : undefined} aria-label={`Camera feed for ${printer.name}`} onClick={size !== 'small' ? onClose : undefined}>
      <div className={containerClasses} onClick={e => e.stopPropagation()}>
        {/* Video */}
        <div className={size === 'fullscreen' ? 'flex-1 relative bg-black' : 'relative aspect-video bg-black rounded-t-xl overflow-hidden'}>
          <video
            ref={videoRef}
            autoPlay
            playsInline
            muted
            aria-label={`Live camera feed for ${printer.name}`}
            className="w-full h-full object-contain"
          />
          {status === 'connecting' && (
            <div className="absolute inset-0 flex items-center justify-center">
              <div className="text-farm-400 text-sm animate-pulse">Connecting...</div>
            </div>
          )}
          {status === 'error' && (
            <div className="absolute inset-0 flex items-center justify-center">
              <div className="text-red-400 text-sm">{error || 'Connection failed'}</div>
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
            <span className="font-medium text-sm">{printer.name}</span>
          </div>
          <span className="text-xs text-farm-500 capitalize">{status}</span>
        </div>
      </div>
    </div>
  )
}
