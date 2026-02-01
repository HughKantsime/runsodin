import { useState, useEffect, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Video, VideoOff, Maximize2, Grid, LayoutGrid } from 'lucide-react'
import CameraModal from '../components/CameraModal'

const API_BASE = window.location.hostname === 'localhost'
  ? 'http://localhost:8000/api'
  : 'http://' + window.location.hostname + ':8000/api'

const API_KEY = '5464389e808f206efd9f9febef7743ff7a16911797cb0f058e805c82b33396ce'

function CameraCard({ camera, onExpand }) {
  const videoRef = useRef(null)
  const pcRef = useRef(null)
  const [status, setStatus] = useState('connecting')

  useEffect(() => {
    startWebRTC()
    return () => {
      if (pcRef.current) {
        pcRef.current.close()
        pcRef.current = null
      }
    }
  }, [camera.id])

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
        'X-API-Key': API_KEY
      }
      if (token) headers['Authorization'] = 'Bearer ' + token

      const response = await fetch(API_BASE + '/cameras/' + camera.id + '/webrtc', {
        method: 'POST',
        headers: headers,
        body: offer.sdp
      })

      if (!response.ok) throw new Error('Failed')

      const answerSDP = await response.text()
      await pc.setRemoteDescription(new RTCSessionDescription({
        type: 'answer',
        sdp: answerSDP
      }))
    } catch (err) {
      setStatus('error')
    }
  }

  const dotColor = status === 'live'
    ? 'bg-green-500'
    : status === 'connecting'
      ? 'bg-yellow-500 animate-pulse'
      : 'bg-red-500'

  return (
    <div className="bg-farm-950 rounded-xl border border-farm-800 overflow-hidden group">
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
            <div className="text-farm-500 text-sm animate-pulse">Connecting...</div>
          </div>
        )}
        {status === 'error' && (
          <div className="absolute inset-0 flex items-center justify-center">
            <VideoOff size={32} className="text-farm-600" />
          </div>
        )}
        <button
          onClick={() => onExpand(camera)}
          className="absolute top-2 right-2 p-1.5 bg-black/50 rounded-lg opacity-0 group-hover:opacity-100 transition-opacity hover:bg-black/80"
        >
          <Maximize2 size={16} />
        </button>
      </div>
      <div className="p-3 flex items-center justify-between">
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

  const gridClass = columns === 1
    ? 'grid-cols-1'
    : columns === 2
      ? 'grid-cols-1 md:grid-cols-2'
      : 'grid-cols-1 md:grid-cols-2 lg:grid-cols-3'

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h2 className="text-2xl font-display font-bold">Cameras</h2>
          <p className="text-sm text-farm-500 mt-1">
            {cameras ? cameras.length : 0} camera{cameras?.length !== 1 ? 's' : ''} available
          </p>
        </div>
        <div className="flex items-center gap-2 bg-farm-900 rounded-lg p-1">
          <button
            onClick={() => setColumns(1)}
            className={'p-2 rounded ' + (columns === 1 ? 'bg-farm-700 text-white' : 'text-farm-400 hover:text-white')}
          >
            <Grid size={16} />
          </button>
          <button
            onClick={() => setColumns(2)}
            className={'p-2 rounded ' + (columns === 2 ? 'bg-farm-700 text-white' : 'text-farm-400 hover:text-white')}
          >
            <LayoutGrid size={16} />
          </button>
          <button
            onClick={() => setColumns(3)}
            className={'p-2 rounded ' + (columns === 3 ? 'bg-farm-700 text-white' : 'text-farm-400 hover:text-white')}
          >
            <Video size={16} />
          </button>
        </div>
      </div>

      {isLoading && (
        <div className="text-center text-farm-500 py-12">Loading cameras...</div>
      )}

      {cameras && cameras.length === 0 && (
        <div className="text-center text-farm-500 py-12">
          <VideoOff size={48} className="mx-auto mb-4 text-farm-600" />
          <p>No cameras configured</p>
          <p className="text-sm mt-1">Add camera URLs in printer settings</p>
        </div>
      )}

      {cameras && cameras.length > 0 && (
        <div className={'grid gap-4 ' + gridClass}>
          {cameras.map(camera => (
            <CameraCard
              key={camera.id}
              camera={camera}
              onExpand={setExpandedCamera}
            />
          ))}
        </div>
      )}

      {expandedCamera && (
        <CameraModal
          printer={expandedCamera}
          onClose={() => setExpandedCamera(null)}
        />
      )}
    </div>
  )
}
