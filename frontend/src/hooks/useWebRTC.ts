import { useState, useEffect, useRef, useCallback } from 'react'

const ICE_SERVERS = [{ urls: 'stun:stun.l.google.com:19302' }]
const MAX_BACKOFF_MS = 30000
const BASE_DELAY_MS = 2000

/** Camera connection lifecycle. maxRetries=0 permits unlimited automatic retries. */
export default function useWebRTC(cameraId, { maxRetries = 5, enabled = true } = {}) {
  const videoRef = useRef(null)
  const pcRef = useRef(null)
  const abortRef = useRef(null)
  const generationRef = useRef(0)
  const activeRef = useRef(false)
  const retryTimerRef = useRef(null)
  const retryCountRef = useRef(0)
  const disconnectGraceRef = useRef(null)
  const [status, setStatus] = useState('connecting')
  const [fallback, setFallback] = useState(false)
  const live = status === 'live'
  useEffect(() => {
    setFallback(false)
    if (!enabled || !cameraId || live) return
    const timer = setTimeout(() => setFallback(true), 12000)
    return () => clearTimeout(timer)
  }, [enabled, cameraId, live])

  const clearTimers = useCallback(() => {
    clearTimeout(retryTimerRef.current)
    retryTimerRef.current = null
    clearTimeout(disconnectGraceRef.current)
    disconnectGraceRef.current = null
  }, [])

  const closePc = useCallback(() => {
    generationRef.current++
    abortRef.current?.abort()
    abortRef.current = null
    if (pcRef.current) {
      pcRef.current.ontrack = null
      pcRef.current.oniceconnectionstatechange = null
      pcRef.current.close()
      pcRef.current = null
    }
    if (videoRef.current) {
      videoRef.current.onloadeddata = null
      videoRef.current.srcObject = null
    }
  }, [])

  const scheduleRetry = useCallback((startFn) => {
    if (!activeRef.current || (maxRetries > 0 && retryCountRef.current >= maxRetries)) return
    clearTimers()
    const generation = generationRef.current
    const delay = Math.min(BASE_DELAY_MS * Math.pow(2, retryCountRef.current), MAX_BACKOFF_MS)
    retryCountRef.current++
    retryTimerRef.current = setTimeout(() => {
      retryTimerRef.current = null
      if (activeRef.current && generationRef.current === generation) startFn()
    }, delay)
  }, [maxRetries, clearTimers])

  const startWebRTC = useCallback(async () => {
    if (!activeRef.current || !enabled || !cameraId) return
    clearTimers()
    closePc()
    const generation = generationRef.current
    let pc = null
    const current = () => activeRef.current && generationRef.current === generation && pcRef.current === pc
    try {
      setStatus('connecting')
      pc = new RTCPeerConnection({ iceServers: ICE_SERVERS })
      pcRef.current = pc
      const controller = new AbortController()
      abortRef.current = controller
      pc.ontrack = (event) => {
        if (!current() || !videoRef.current) return
        const stream = event.streams[0] || (event.track ? new MediaStream([event.track]) : null)
        if (!stream) return
        videoRef.current.onloadeddata = () => {
          if (!current()) return
          setStatus('live')
          retryCountRef.current = 0
        }
        videoRef.current.srcObject = stream
      }
      pc.oniceconnectionstatechange = () => {
        if (!current()) return
        const state = pc.iceConnectionState
        if (state === 'connected' || state === 'completed') {
          clearTimeout(disconnectGraceRef.current)
          disconnectGraceRef.current = null
          if (videoRef.current?.srcObject && videoRef.current.readyState >= 2) setStatus('live')
        } else if (state === 'disconnected') {
          setStatus('disconnected')
          clearTimeout(disconnectGraceRef.current)
          disconnectGraceRef.current = setTimeout(() => {
            if (!current() || pc.iceConnectionState !== 'disconnected') return
            closePc()
            scheduleRetry(startWebRTC)
          }, 5000)
        } else if (state === 'failed' || state === 'closed') {
          clearTimers()
          closePc()
          setStatus('disconnected')
          scheduleRetry(startWebRTC)
        }
      }
      pc.addTransceiver('video', { direction: 'recvonly' })
      const offer = await pc.createOffer()
      if (!current()) return
      await pc.setLocalDescription(offer)
      if (!current()) return
      const response = await fetch(`/api/cameras/${cameraId}/webrtc`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/sdp' },
        credentials: 'include',
        body: offer.sdp,
        signal: controller.signal,
      })
      if (!current()) return
      if (!response.ok) throw new Error('Camera signaling failed')
      const answerSDP = await response.text()
      if (!current()) return
      await pc.setRemoteDescription(new RTCSessionDescription({ type: 'answer', sdp: answerSDP }))
    } catch {
      if (!current()) return
      closePc()
      setStatus('error')
      scheduleRetry(startWebRTC)
    }
  }, [cameraId, enabled, closePc, clearTimers, scheduleRetry])

  useEffect(() => {
    activeRef.current = Boolean(enabled && cameraId)
    retryCountRef.current = 0
    if (activeRef.current) startWebRTC()
    else {
      clearTimers()
      closePc()
      setStatus('connecting')
    }
    return () => {
      activeRef.current = false
      clearTimers()
      closePc()
    }
  }, [cameraId, enabled, startWebRTC, closePc, clearTimers])

  const retry = useCallback(() => {
    if (!activeRef.current || !enabled || !cameraId) return
    clearTimers()
    retryCountRef.current = 0
    startWebRTC()
  }, [cameraId, enabled, clearTimers, startWebRTC])

  return { videoRef, status, retry, fallback }
}
