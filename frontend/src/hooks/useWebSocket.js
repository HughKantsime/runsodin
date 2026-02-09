import { useEffect, useRef, useCallback } from 'react'
import { useQueryClient } from '@tanstack/react-query'

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

  const connect = useCallback(() => {
    // Don't reconnect too fast
    const now = Date.now()
    if (now - lastConnectAttempt.current < 3000) return
    lastConnectAttempt.current = now

    // Build WebSocket URL from current location
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const url = `${proto}//${window.location.host}/ws`

    try {
      const ws = new WebSocket(url)
      wsRef.current = ws

      ws.onopen = () => {
        console.log('[WS] Connected')
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
              break
          }
        } catch (e) {
          // Ignore parse errors
        }
      }

      ws.onclose = () => {
        console.log('[WS] Disconnected, reconnecting in 5s...')
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
