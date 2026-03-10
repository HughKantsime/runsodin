import { useState, useEffect, useRef, useCallback } from 'react'

const ICE_SERVERS = [{ urls: 'stun:stun.l.google.com:19302' }]
const API_BASE = '/api'
const MAX_BACKOFF_MS = 30000
const BASE_DELAY_MS = 2000

/**
 * Shared WebRTC hook for camera streaming via go2rtc.
 *
 * Fixes addressed by centralising here:
 *  - Retry cap enforced on BOTH ICE-failure and signaling-error paths
 *  - `disconnected` ICE state given a grace period before retrying
 *    (it is transient and can self-recover to `connected`)
 *  - Pending retry timers cleared before scheduling new ones
 *    (prevents concurrent PeerConnections)
 *  - Old PeerConnection always closed before creating a new one
 *
 * @param {number|string|null} cameraId  Printer / camera ID
 * @param {object}  opts
 * @param {number}  opts.maxRetries   Max auto-retry attempts (default 5, 0 = no auto-retry)
 * @param {boolean} opts.enabled      Set false to skip connecting (default true)
 * @returns {{ videoRef, status, retry }}
 */
export default function useWebRTC(cameraId, { maxRetries = 5, enabled = true } = {}) {
  const videoRef = useRef(null)
  const pcRef = useRef(null)
  const retryTimerRef = useRef(null)
  const retryCountRef = useRef(0)
  const disconnectGraceRef = useRef(null)
  const [status, setStatus] = useState('connecting')

  // Clean up a PeerConnection
  const closePc = useCallback(() => {
    if (pcRef.current) {
      pcRef.current.close()
      pcRef.current = null
    }
  }, [])

  // Clear any pending retry / grace timers
  const clearTimers = useCallback(() => {
    clearTimeout(retryTimerRef.current)
    retryTimerRef.current = null
    clearTimeout(disconnectGraceRef.current)
    disconnectGraceRef.current = null
  }, [])

  // Schedule a retry with exponential backoff (respects cap)
  const scheduleRetry = useCallback((startFn) => {
    if (retryCountRef.current >= maxRetries) return
    clearTimers()
    const delay = Math.min(BASE_DELAY_MS * Math.pow(2, retryCountRef.current), MAX_BACKOFF_MS)
    retryCountRef.current++
    retryTimerRef.current = setTimeout(startFn, delay)
  }, [maxRetries, clearTimers])

  const startWebRTC = useCallback(async () => {
    if (!cameraId) return
    try {
      clearTimers()
      closePc()
      setStatus('connecting')

      const pc = new RTCPeerConnection({ iceServers: ICE_SERVERS })
      pcRef.current = pc

      pc.ontrack = (event) => {
        if (videoRef.current) {
          videoRef.current.srcObject = event.streams[0]
          setStatus('live')
          retryCountRef.current = 0 // reset on success
        }
      }

      pc.oniceconnectionstatechange = () => {
        const state = pc.iceConnectionState
        if (state === 'connected' || state === 'completed') {
          // If we were in a grace period, cancel it — connection recovered
          clearTimeout(disconnectGraceRef.current)
          disconnectGraceRef.current = null
        } else if (state === 'disconnected') {
          // `disconnected` is transient — give it 5s to recover before retrying
          clearTimeout(disconnectGraceRef.current)
          disconnectGraceRef.current = setTimeout(() => {
            // Still disconnected after grace period → treat as failure
            if (pcRef.current && pcRef.current.iceConnectionState === 'disconnected') {
              setStatus('disconnected')
              scheduleRetry(startWebRTC)
            }
          }, 5000)
        } else if (state === 'failed') {
          clearTimeout(disconnectGraceRef.current)
          disconnectGraceRef.current = null
          setStatus('disconnected')
          scheduleRetry(startWebRTC)
        }
      }

      pc.addTransceiver('video', { direction: 'recvonly' })
      const offer = await pc.createOffer()
      await pc.setLocalDescription(offer)

      const response = await fetch(`${API_BASE}/cameras/${cameraId}/webrtc`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/sdp' },
        credentials: 'include',
        body: offer.sdp,
      })
      if (!response.ok) throw new Error(`Signaling failed: ${response.status}`)

      const answerSDP = await response.text()
      await pc.setRemoteDescription(new RTCSessionDescription({ type: 'answer', sdp: answerSDP }))
    } catch {
      setStatus('error')
      scheduleRetry(startWebRTC)
    }
  }, [cameraId, closePc, clearTimers, scheduleRetry])

  // Connect / disconnect lifecycle
  useEffect(() => {
    if (!enabled || !cameraId) {
      closePc()
      clearTimers()
      setStatus('connecting')
      return
    }
    retryCountRef.current = 0
    startWebRTC()
    return () => {
      clearTimers()
      closePc()
    }
  }, [cameraId, enabled, startWebRTC, closePc, clearTimers])

  // Manual retry — resets counter
  const retry = useCallback(() => {
    clearTimers()
    retryCountRef.current = 0
    startWebRTC()
  }, [clearTimers, startWebRTC])

  return { videoRef, status, retry }
}
