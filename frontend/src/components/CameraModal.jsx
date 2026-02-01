import { useState, useEffect, useRef } from 'react'
import { X } from 'lucide-react'

export default function CameraModal({ printer, onClose }) {
  const videoRef = useRef(null)
  const pcRef = useRef(null)
  const [status, setStatus] = useState('connecting')
  const [error, setError] = useState(null)

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

      const API_BASE = '/api'

      const token = localStorage.getItem('token')
      const headers = {
        'Content-Type': 'application/sdp',
        'X-API-Key': '5464389e808f206efd9f9febef7743ff7a16911797cb0f058e805c82b33396ce',

      }
      if (token) headers['Authorization'] = 'Bearer ' + token

      const response = await fetch(API_BASE + '/cameras/' + printer.id + '/webrtc', {
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
    ? 'bg-green-500 animate-pulse'
    : status === 'connecting'
      ? 'bg-yellow-500 animate-pulse'
      : 'bg-red-500'

  return (
    <div className="fixed inset-0 bg-black/80 z-50 flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-farm-950 rounded-xl border border-farm-800 w-full max-w-4xl" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between p-4 border-b border-farm-800">
          <div className="flex items-center gap-3">
            <div className={'w-2 h-2 rounded-full ' + dotColor} />
            <h3 className="font-display font-semibold text-lg">{printer.name}</h3>
            <span className="text-sm text-farm-500 capitalize">{status}</span>
          </div>
          <button onClick={onClose} className="p-2 hover:bg-farm-800 rounded-lg transition-colors">
            <X size={20} />
          </button>
        </div>
        <div className="relative aspect-video bg-black">
          <video
            ref={videoRef}
            autoPlay
            playsInline
            muted
            className="w-full h-full object-contain"
          />
          {status === 'connecting' && (
            <div className="absolute inset-0 flex items-center justify-center">
              <div className="text-farm-400 text-sm">Connecting to camera...</div>
            </div>
          )}
          {status === 'error' && (
            <div className="absolute inset-0 flex items-center justify-center">
              <div className="text-red-400 text-sm">{error || 'Connection failed'}</div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
