import { useEffect, useRef, useCallback } from 'react'
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
export default function useWebSocket() {
  const queryClient = useQueryClient()
  const wsRef = useRef(null)
  const reconnectTimer = useRef(null)
  const lastConnectAttempt = useRef(0)

  const connect = useCallback(async () => {
    // Don't reconnect too fast
    const now = Date.now()
    if (now - lastConnectAttempt.current < 3000) return
    lastConnectAttempt.current = now

    // Fetch a short-lived WS token (WebSocket connections can't send cookies/headers).
    // Falls back to no token — backend allows unauthenticated WS when API key is disabled.
    let wsToken = ''
    try {
      const res = await fetch('/api/auth/ws-token', { method: 'POST', credentials: 'include' })
      if (res.ok) {
        const data = await res.json()
        wsToken = data.token || ''
      }
    } catch {}

    // Build WebSocket URL from current location
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const tokenParam = wsToken ? `?token=${encodeURIComponent(wsToken)}` : ''
    const url = `${proto}//${window.location.host}/ws${tokenParam}`

    try {
      const ws = new WebSocket(url)
      wsRef.current = ws

      ws.onopen = () => {
        // Connected
        if (reconnectTimer.current) {
          clearTimeout(reconnectTimer.current)
          reconnectTimer.current = null
        }
      }

      ws.onmessage = (event) => {
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
              // Update the specific printer in the cached printers list
              queryClient.setQueryData(['printers'], (old) => {
                if (!old || !Array.isArray(old)) return old
                return old.map(p => {
                  if (p.id !== d.printer_id) return p
                  return {
                    ...p,
                    bed_temp: d.bed_temp ?? p.bed_temp,
                    bed_target_temp: d.bed_target ?? p.bed_target_temp,
                    nozzle_temp: d.nozzle_temp ?? p.nozzle_temp,
                    nozzle_target_temp: d.nozzle_target ?? p.nozzle_target_temp,
                    gcode_state: d.state ?? p.gcode_state,
                    mc_percent: d.progress ?? p.mc_percent,
                    mc_remaining_time: d.remaining_min ?? p.mc_remaining_time,
                    layer_num: d.current_layer ?? p.layer_num,
                    total_layer_num: d.total_layers ?? p.total_layer_num,
                    gcode_file: d.gcode_file ?? p.gcode_file,
                    h2d_nozzles: d.h2d_nozzles ?? p.h2d_nozzles,
                    external_spools: d.external_spools ?? p.external_spools,
                    last_seen: new Date().toISOString().replace('T', ' ').replace('Z', '').split('.')[0],
                  }
                })
              })
              // Update print jobs progress
              if (d.progress != null) {
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

            case 'alert_new':
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

      ws.onclose = () => {
        // Disconnected — reconnecting in 5s
        wsRef.current = null
        reconnectTimer.current = setTimeout(connect, 5000)
      }

      ws.onerror = () => {
        // onclose will fire after this
        ws.close()
      }

    } catch (e) {
      console.error('[WS] Connection failed:', e)
      reconnectTimer.current = setTimeout(connect, 5000)
    }
  }, [queryClient])

  useEffect(() => {
    requestNotificationPermission()
  }, [])

  useEffect(() => {
    connect()
    return () => {
      if (wsRef.current) {
        wsRef.current.close()
        wsRef.current = null
      }
      if (reconnectTimer.current) {
        clearTimeout(reconnectTimer.current)
      }
    }
  }, [connect])
}
