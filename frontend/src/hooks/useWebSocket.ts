import { useEffect, useRef, useCallback } from 'react'
import { mergePrinter } from '../utils/printerLiveValues'
import type { Printer } from '../types/printer'
import { useQueryClient } from '@tanstack/react-query'

function requestNotificationPermission() {
  if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission()
  }
}

function sendNotification(title, body) {
  if (!('Notification' in window) || Notification.permission !== 'granted') return
  if (document.visibilityState === 'visible') return
  new Notification(title, { body, icon: '/odin-icon-192.svg', tag: title })
}

/**
 * WebSocket hook for real-time O.D.I.N. updates.
 * 
 * Connects to /ws endpoint. On receiving events, updates React Query cache
 * directly so existing components re-render without polling.
 * 
 * Event types handled:
 * - printer_telemetry: updates printer data in cache
 * - job_started / job_completed: invalidates job queries
 * - alert_new: invalidates alert queries
 */
/** Wait before asking for a live-updates token again: 5 s, 10 s, 20 s, 40 s, then 60 s. */
export function tokenRetryDelayMs(failures: number): number {
  return Math.min(60000, 5000 * 2 ** Math.max(0, Math.min(failures, 10)))
}

export default function useWebSocket() {
  const queryClient = useQueryClient()
  const wsRef = useRef(null)
  const reconnectTimer = useRef(null)
  const lastConnectAttempt = useRef(0)
  const tokenFailures = useRef(0)
  const stopped = useRef(false)

  const silenceTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const refreshPrinters = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['printers'] })
    queryClient.invalidateQueries({ queryKey: ['printer'] })
  }, [queryClient])

  const connect = useCallback(async () => {
    if (stopped.current) return
    // Don't reconnect too fast
    const now = Date.now()
    if (now - lastConnectAttempt.current < 3000) return
    lastConnectAttempt.current = now

    // Fetch a short-lived WS token (WebSocket connections can't send cookies/headers).
    // A missing token fails closed; the backend never accepts an anonymous socket.
    let wsToken = ''
    let signedOut = false
    try {
      const res = await fetch('/api/auth/ws-token', { method: 'POST', credentials: 'include' })
      if (res.ok) {
        const data = await res.json()
        wsToken = data.token || ''
      } else if (res.status === 401 || res.status === 403) {
        signedOut = true
      }
    } catch {}
    if (stopped.current) return

    // Build WebSocket URL from current location
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    if (!wsToken) {
      // No token (rate limited, server restarting, network blip): try again later, backing
      // off from 5 s to 60 s, so the page gets its live updates back without a reload.
      // A refused session (401/403) is not retried: the app is about to show the sign-in page.
      if (!signedOut) {
        const delay = tokenRetryDelayMs(tokenFailures.current)
        tokenFailures.current += 1
        if (reconnectTimer.current) clearTimeout(reconnectTimer.current)
        reconnectTimer.current = setTimeout(connect, delay)
      }
      return
    }
    tokenFailures.current = 0
    const tokenParam = `?token=${encodeURIComponent(wsToken)}`
    const url = `${proto}//${window.location.host}/ws${tokenParam}`

    try {
      const ws = new WebSocket(url)
      wsRef.current = ws

      const reconnect = () => {
        // A previous effect's socket may close after its replacement has connected.
        if (wsRef.current !== ws) return
        clearTimeout(silenceTimer.current)
        // Disconnected — reconnecting in 5s
        wsRef.current = null
        if (stopped.current) return
        reconnectTimer.current = setTimeout(connect, 5000)
      }

      const recordMessage = () => {
        clearTimeout(silenceTimer.current)
        silenceTimer.current = setTimeout(() => {
          if (wsRef.current !== ws) return
          // A broken connection may never finish the browser's close handshake.
          reconnect()
          ws.close()
        }, 75000)
      }
      recordMessage()
      ws.onopen = () => {
        if (wsRef.current !== ws) return
        recordMessage()
        refreshPrinters()
        if (reconnectTimer.current) {
          clearTimeout(reconnectTimer.current)
          reconnectTimer.current = null
        }
      }

      ws.onmessage = (event) => {
        if (wsRef.current !== ws) return
        recordMessage()
        if (event.data === 'pong') return
        try {
          const msg = JSON.parse(event.data)
          
          if (msg.type === 'ping') {
            ws.send('ping')
            return
          }

          const d = msg.data
          if (!d) return

          switch (msg.type) {
            case 'printer_telemetry':
              const updatePrinter = (p: Printer) => {
                if (p.id !== d.printer_id) return p
                const keepQuietFailure = d.state === 'FAILED' && p.gcode_state === 'FAILED'
                  && p.state_needs_attention === false && d.state_needs_attention === undefined
                return mergePrinter(p, {
                  ...p,
                  bed_temp: d.bed_temp ?? p.bed_temp,
                  bed_target_temp: d.bed_target ?? p.bed_target_temp,
                  nozzle_temp: d.nozzle_temp ?? p.nozzle_temp,
                  nozzle_target_temp: d.nozzle_target ?? p.nozzle_target_temp,
                  gcode_state: d.state ?? p.gcode_state,
                  ...(d.state != null && !keepQuietFailure && {
                    state_needs_attention: d.state_needs_attention,
                  }),
                  ...(d.gcode_file != null && { gcode_file: d.gcode_file }),
                  ...(d.h2d_nozzles != null && { h2d_nozzles: d.h2d_nozzles }),
                  ...(d.external_spools != null && { external_spools: d.external_spools }),
                  ...('progress' in d && { mc_percent: d.progress }),
                  ...('remaining_min' in d && { mc_remaining_time: d.remaining_min }),
                  ...('current_layer' in d && { layer_num: d.current_layer }),
                  ...('total_layers' in d && { total_layer_num: d.total_layers }),
                  ...('speed_level' in d && { speed_level: d.speed_level }),
                  ...('speed_percent' in d && { speed_percent: d.speed_percent }),
                  telemetry_at: d.telemetry_at ?? null,
                  last_seen: d.telemetry_at ?? new Date().toISOString(),
                })
              }
              queryClient.setQueriesData<Printer[]>({ queryKey: ['printers'] }, old =>
                Array.isArray(old) ? old.map(updatePrinter) : old)
              queryClient.setQueriesData<Printer>({ queryKey: ['printer'] }, old =>
                old ? updatePrinter(old) : old)
              // Update print jobs progress
              if ([d.progress, d.remaining_min, d.current_layer, d.total_layers].some(value => value != null)) {
                queryClient.setQueryData(['print-jobs'], (old) => {
                  if (!old || !Array.isArray(old)) return old
                  return old.map(j => {
                    if (j.printer_id !== d.printer_id || j.status !== 'running') return j
                    return {
                      ...j,
                      progress_percent: d.progress ?? j.progress_percent,
                      remaining_minutes: d.remaining_min ?? j.remaining_minutes,
                      current_layer: d.current_layer ?? j.current_layer,
                      total_layers: d.total_layers ?? j.total_layers,
                    }
                  })
                })
              }
              break

            case 'job_started':
            case 'job_completed':
              // Refetch jobs and printers — state changed significantly
              queryClient.invalidateQueries({ queryKey: ['print-jobs'] })
              queryClient.invalidateQueries({ queryKey: ['jobs'] })
              queryClient.invalidateQueries({ queryKey: ['printers'] })
              queryClient.invalidateQueries({ queryKey: ['stats'] })
              break

            case 'restart_question_changed':
              queryClient.invalidateQueries({ queryKey: ['print-jobs'] })
              queryClient.invalidateQueries({ queryKey: ['dispatch-recovery', d.printer_id] })
              queryClient.invalidateQueries({ queryKey: ['alerts'] })
              queryClient.invalidateQueries({ queryKey: ['alert-summary'] })
              break

            case 'alert_new':
              if (d.restart_question_id) {
                queryClient.invalidateQueries({ queryKey: ['dispatch-recovery', d.printer_id] })
                queryClient.invalidateQueries({ queryKey: ['print-jobs'] })
              }
              // Refetch alert counts
              queryClient.invalidateQueries({ queryKey: ['dash-alert-summary'] })
              queryClient.invalidateQueries({ queryKey: ['alert-summary'] })
              queryClient.invalidateQueries({ queryKey: ['alerts'] })
              sendNotification(d.title || 'O.D.I.N. Alert', d.message || '')
              break

            case 'vision_detection':
              queryClient.invalidateQueries({ queryKey: ['vision-detections'] })
              queryClient.invalidateQueries({ queryKey: ['vision-stats'] })
              queryClient.invalidateQueries({ queryKey: ['alerts'] })
              sendNotification(
                'Vigil AI Detection',
                `${d.detection_type || 'Issue'} detected on Printer #${d.printer_id}`
              )
              break

            case 'job_dispatch_event':
              // Invalidate jobs on terminal dispatch statuses
              if (d.status === 'dispatched' || d.status === 'failed') {
                queryClient.invalidateQueries({ queryKey: ['jobs'] })
              }
              break
          }
        } catch (e) {
          // Ignore parse errors
        }
      }

      ws.onclose = reconnect

      ws.onerror = () => {
        // onclose will fire after this
        ws.close()
      }

    } catch (e) {
      console.error('[WS] Connection failed:', e)
      reconnectTimer.current = setTimeout(connect, 5000)
    }
  }, [queryClient, refreshPrinters])

  useEffect(() => {
    requestNotificationPermission()
  }, [])

  useEffect(() => {
    stopped.current = false
    connect()
    const onVisible = () => { if (document.visibilityState === 'visible') refreshPrinters() }
    window.addEventListener('online', refreshPrinters)
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      window.removeEventListener('online', refreshPrinters)
      document.removeEventListener('visibilitychange', onVisible)
      clearTimeout(silenceTimer.current)
      stopped.current = true
      if (wsRef.current) {
        wsRef.current.close()
        wsRef.current = null
      }
      if (reconnectTimer.current) {
        clearTimeout(reconnectTimer.current)
      }
    }
  }, [connect, refreshPrinters])
}
