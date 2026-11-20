import type { Printer } from '../../types'
import PageState from '../ui/PageState'
import { useState, useEffect, useRef } from 'react'
import { StopCircle, Pause, Play, X, AlertTriangle } from 'lucide-react'
import toast from 'react-hot-toast'
import { fetchAPI } from '../../api'
import { canDo, getCurrentUser } from '../../permissions'
import { isOnline, isActivePrintState, isPausedState, isPrintingState } from '../../utils/shared'

import { controlLabel, pendingMessage, unavailableMessage, unprovenHelp, type ControlReceipt } from '../../utils/printControl'
import type { PrintControlAction } from '../../types/printer'

type ActivePrinter = Printer

/**
 * Emergency Stop Button - Floating button that shows when any printer is actively printing.
 * Provides quick access to stop, pause, or resume prints.
 */
export default function EmergencyStop() {
  const [printers, setPrinters] = useState<ActivePrinter[]>([])
  const [showPanel, setShowPanel] = useState(false)
  const [loading, setLoading] = useState<Record<string, boolean>>({})
  const [confirmStop, setConfirmStop] = useState<number | null>(null)
  const [listError, setListError] = useState(false)
  const [outcome, setOutcome] = useState('')
  const busy = useRef(new Set<string>())
  const latest = useRef(new Map<number, symbol>())
  const mounted = useRef(false)

  // Fetch printer status
  useEffect(() => {
    mounted.current = true
    let active = true
    let sequence = 0
    const fetchPrinters = async () => {
      const request = ++sequence
      try {
        const data = await fetchAPI('/printers')
        if (!Array.isArray(data)) throw new Error('Invalid printer list')
        if (!active || request !== sequence) return
        setListError(false)
        setPrinters(data.filter((p: ActivePrinter) => p.pending_control || (isOnline(p) && isActivePrintState(p.gcode_state))))
      } catch {
        if (!active || request !== sequence) return
        setListError(true)
        setPrinters([])
        setConfirmStop(null)
      }
    }

    fetchPrinters()
    const interval = setInterval(fetchPrinters, 5000)
    return () => { active = false; mounted.current = false; clearInterval(interval) }
  }, [])

  const sendCommand = async (printerId: number, action: PrintControlAction) => {
    const target = printers.find(p => p.id === printerId)
    const key = target?.api_type === 'moonraker' ? printerId + action : String(printerId)
    const principal = getCurrentUser()
    if (!principal || !['operator', 'admin'].includes(principal.role) || !canDo('printers.edit') || listError || busy.current.has(key)) return
    busy.current.add(key)
    const request = Symbol()
    latest.current.set(printerId, request)
    const identity = JSON.stringify(principal)
    const stillCurrent = () => mounted.current && JSON.stringify(getCurrentUser()) === identity
    setLoading(prev => ({ ...prev, [printerId + action]: true }))

    try {
      const receipt = await fetchAPI<ControlReceipt>(`/printers/${printerId}/${action}`, { method: 'POST' })
      if (!stillCurrent()) return
      if (latest.current.get(printerId) !== request) return
      if (receipt?.outcome) {
        setPrinters(rows => rows.map(p => p.id === printerId ? { ...p, pending_control: receipt.pending_control || null } : p))
      }
      let message = receipt?.success === true ? receipt.message || 'Command accepted; printer confirmation pending' : 'Command not confirmed'
      if (receipt?.unproven && action === 'stop') message += ' The printer unwinds filament for up to a few minutes before it is idle.'
      setOutcome(message)
    } catch (error) {
      if (!stillCurrent() || latest.current.get(printerId) !== request) return
      setOutcome(target?.api_type === 'moonraker' && error instanceof Error ? error.message : 'Command not confirmed')
      toast.error('Printer command failed')
    } finally {
      busy.current.delete(key)
      if (stillCurrent()) {
        setLoading(prev => ({ ...prev, [printerId + action]: false }))
        if (action === 'stop') setConfirmStop(null)
      }
    }
  }

  // Don't show if no active prints
  const user = getCurrentUser()
  if (!user || !['operator', 'admin'].includes(user.role) || !canDo('printers.edit')) return null

  const activePrinting = printers.filter(p => isPrintingState(p.gcode_state)).length

  return (
    <>
      {listError && <PageState kind="error" role="alert">Printer status unavailable. Controls are disabled until status refreshes.</PageState>}
      {outcome && <div role="status">{outcome}</div>}
      {/* Floating Button */}
      <button
        onClick={() => setShowPanel(!showPanel)}
        className={`emergency-controls-toggle fixed bottom-[calc(3.5rem+env(safe-area-inset-bottom)+0.75rem)] md:bottom-6 right-6 z-40 flex items-center gap-2 px-4 py-3 rounded-md shadow-lg transition-all ${
          showPanel
            ? 'bg-[var(--brand-card-bg)] text-white'
            : 'bg-[var(--brand-content-bg)] border border-red-500/30 text-red-400 hover:bg-red-600 hover:text-white'
        }`}
        aria-label={showPanel ? "Close emergency controls" : `Emergency controls, ${activePrinting} active prints`}
      >
        {showPanel ? (
          <X size={20} />
        ) : (
          <>
            <StopCircle size={20} />
            <span className="font-medium">{activePrinting} Active</span>
          </>
        )}
      </button>

      {/* Control Panel */}
      {showPanel && (
        <div className="emergency-controls-panel fixed bottom-[calc(3.5rem+env(safe-area-inset-bottom)+4rem)] md:bottom-20 right-6 z-40 w-80 bg-[var(--brand-card-bg)] border border-[var(--brand-card-border)] rounded-md shadow-2xl overflow-hidden" role="region" aria-label="Emergency printer controls">
          <div className="p-3 bg-red-900/50 border-b border-red-800 flex items-center gap-2">
            <AlertTriangle className="text-red-400" size={18} aria-hidden="true" />
            <span className="font-medium text-red-200">Emergency Controls</span>
          </div>

          <div className="p-3 space-y-2 max-h-80 overflow-y-auto">
            {!listError && printers.length === 0 && <p>No active prints</p>}
            {printers.map(printer => {
              const isPaused = isPausedState(printer.gcode_state)
              const isLoading = (action: PrintControlAction) => loading[printer.id + action]
              const disabled = (action: PrintControlAction) => printer.pending_control?.action === action ||
                (printer.api_type === 'moonraker' ? isLoading(action) : busy.current.has(String(printer.id)))
              const unavailable = (action: PrintControlAction) => printer.unavailable_controls?.includes(action)
              const toggle = isPaused ? 'resume' : 'pause'

              return (
                <div key={printer.id} className="p-3 bg-[var(--brand-card-border)] rounded-md">
                  <div className="flex items-center justify-between mb-2">
                    <span className="font-medium text-sm">{printer.nickname || printer.name}</span>
                    <span className={`text-xs px-2 py-0.5 rounded-md ${
                      isPaused ? 'bg-yellow-900/50 text-yellow-400' : 'bg-green-900/50 text-green-400'
                    }`}>
                      {!isOnline(printer) ? 'Offline' : isPaused ? 'Paused' : isPrintingState(printer.gcode_state) ? 'Printing' : printer.print_stage || 'Idle'}
                    </span>
                  </div>

                  {pendingMessage(printer) && <p>{pendingMessage(printer)}</p>}
                  {isOnline(printer) && isActivePrintState(printer.gcode_state) && <>
                  <div className="flex gap-2">
                    {unavailable(toggle) ? <p>{unavailableMessage(toggle)}</p> : isPaused ? (
                      <button
                        onClick={() => sendCommand(printer.id, 'resume')}
                        disabled={disabled('resume')}
                        className="flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 bg-green-700 hover:bg-green-600 rounded-md text-sm transition-colors disabled:opacity-50"
                      >
                        <Play size={14} />
                        {isLoading('resume') ? 'Resuming...' : controlLabel(printer, 'resume')}
                      </button>
                    ) : (
                      <button
                        onClick={() => sendCommand(printer.id, 'pause')}
                        disabled={disabled('pause')}
                        className="flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 bg-yellow-700 hover:bg-yellow-600 rounded-md text-sm transition-colors disabled:opacity-50"
                      >
                        <Pause size={14} />
                        {isLoading('pause') ? 'Pausing...' : controlLabel(printer, 'pause')}
                      </button>
                    )}

                    {unavailable('stop') ? <p>{unavailableMessage('stop')}</p> : confirmStop === printer.id ? (
                      <button
                        onClick={() => sendCommand(printer.id, 'stop')}
                        disabled={disabled('stop')}
                        className="flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 bg-red-600 text-white hover:bg-red-500 rounded-md text-sm transition-colors disabled:opacity-50 animate-pulse"
                      >
                        <StopCircle size={14} />
                        {isLoading('stop') ? 'Stopping...' : 'Confirm ' + controlLabel(printer, 'stop')}
                      </button>
                    ) : (
                      <button
                        onClick={() => setConfirmStop(printer.id)}
                        disabled={disabled('stop')}
                        className="flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 bg-red-900 hover:bg-red-800 rounded-md text-sm transition-colors disabled:opacity-50"
                      >
                        <StopCircle size={14} />
                        {controlLabel(printer, 'stop')}
                      </button>
                    )}
                  </div>
                  {(printer.unproven_controls?.includes(toggle) || printer.unproven_controls?.includes('stop')) && <p>{unprovenHelp}</p>}
                  </>}
                </div>
              )
            })}
          </div>

          <div className="p-2 bg-[var(--brand-card-bg)] text-xs text-[var(--brand-text-muted)] text-center">
            Stop will cancel the print permanently
          </div>
        </div>
      )}
    </>
  )
}
