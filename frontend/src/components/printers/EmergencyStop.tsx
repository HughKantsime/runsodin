import { useState, useEffect, useRef } from 'react'
import { StopCircle, Pause, Play, X, AlertTriangle } from 'lucide-react'
import toast from 'react-hot-toast'
import { fetchAPI } from '../../api'
import { canDo, getCurrentUser } from '../../permissions'
import { isOnline } from '../../utils/shared'

interface ActivePrinter {
  id: number
  name: string
  nickname?: string
  gcode_state: string
  [key: string]: any // TODO: type this properly
}

/**
 * Emergency Stop Button - Floating button that shows when any printer is actively printing.
 * Provides quick access to stop, pause, or resume prints.
 */
export default function EmergencyStop() {
  const [printers, setPrinters] = useState<ActivePrinter[]>([])
  const [showPanel, setShowPanel] = useState(false)
  const [loading, setLoading] = useState<Record<number, string | null>>({})
  const [confirmStop, setConfirmStop] = useState<number | null>(null)
  const [listError, setListError] = useState(false)
  const [outcome, setOutcome] = useState('')
  const busy = useRef(new Set<number>())
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
        setPrinters(data.filter((p: ActivePrinter) => isOnline(p) && (
          p.gcode_state === 'RUNNING' ||
          p.gcode_state === 'PRINTING' ||
          p.gcode_state === 'PAUSED' ||
          p.gcode_state === 'PAUSE')
        ))
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

  const sendCommand = async (printerId: number, action: string) => {
    const principal = getCurrentUser()
    if (!principal || !['operator', 'admin'].includes(principal.role) || !canDo('printers.edit') || listError || busy.current.has(printerId)) return
    busy.current.add(printerId)
    const identity = JSON.stringify(principal)
    const stillCurrent = () => mounted.current && JSON.stringify(getCurrentUser()) === identity
    setLoading(prev => ({ ...prev, [printerId]: action }))

    try {
      const receipt = await fetchAPI(`/printers/${printerId}/${action}`, { method: 'POST' })
      if (!stillCurrent()) return
      setOutcome(receipt?.success === true ? 'Command accepted; printer confirmation pending' : 'Command not confirmed')
    } catch {
      if (!stillCurrent()) return
      setOutcome('Command not confirmed')
      toast.error('Printer command failed')
    } finally {
      busy.current.delete(printerId)
      if (stillCurrent()) {
        setLoading(prev => ({ ...prev, [printerId]: null }))
        setConfirmStop(null)
      }
    }
  }

  // Don't show if no active prints
  const user = getCurrentUser()
  if (!user || !['operator', 'admin'].includes(user.role) || !canDo('printers.edit')) return null

  const activePrinting = printers.filter(p =>
    p.gcode_state === 'RUNNING' || p.gcode_state === 'PRINTING'
  ).length

  return (
    <>
      {listError && <div role="alert">Printer status unavailable. Controls are disabled until status refreshes.</div>}
      {outcome && <div role="status">{outcome}</div>}
      {/* Floating Button */}
      <button
        onClick={() => setShowPanel(!showPanel)}
        className={`fixed bottom-6 right-6 z-40 flex items-center gap-2 px-4 py-3 rounded-md shadow-lg transition-all ${
          showPanel
            ? 'bg-[var(--brand-card-bg)] text-white'
            : 'border border-red-500/30 text-red-400 hover:bg-red-600 hover:text-white'
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
        <div className="fixed bottom-20 right-6 z-40 w-80 bg-[var(--brand-card-bg)] border border-[var(--brand-card-border)] rounded-md shadow-2xl overflow-hidden" role="region" aria-label="Emergency printer controls">
          <div className="p-3 bg-red-900/50 border-b border-red-800 flex items-center gap-2">
            <AlertTriangle className="text-red-400" size={18} aria-hidden="true" />
            <span className="font-medium text-red-200">Emergency Controls</span>
          </div>

          <div className="p-3 space-y-2 max-h-80 overflow-y-auto">
            {!listError && printers.length === 0 && <p>No active prints</p>}
            {printers.map(printer => {
              const isPaused = printer.gcode_state === 'PAUSED' || printer.gcode_state === 'PAUSE'
              const isLoading = loading[printer.id]

              return (
                <div key={printer.id} className="p-3 bg-[var(--brand-card-border)] rounded-md">
                  <div className="flex items-center justify-between mb-2">
                    <span className="font-medium text-sm">{printer.nickname || printer.name}</span>
                    <span className={`text-xs px-2 py-0.5 rounded-md ${
                      isPaused ? 'bg-yellow-900/50 text-yellow-400' : 'bg-green-900/50 text-green-400'
                    }`}>
                      {isPaused ? 'Paused' : 'Printing'}
                    </span>
                  </div>

                  <div className="flex gap-2">
                    {isPaused ? (
                      <button
                        onClick={() => sendCommand(printer.id, 'resume')}
                        disabled={!!isLoading}
                        className="flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 bg-green-700 hover:bg-green-600 rounded-md text-sm transition-colors disabled:opacity-50"
                      >
                        <Play size={14} />
                        {isLoading === 'resume' ? 'Resuming...' : 'Resume'}
                      </button>
                    ) : (
                      <button
                        onClick={() => sendCommand(printer.id, 'pause')}
                        disabled={!!isLoading}
                        className="flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 bg-yellow-700 hover:bg-yellow-600 rounded-md text-sm transition-colors disabled:opacity-50"
                      >
                        <Pause size={14} />
                        {isLoading === 'pause' ? 'Pausing...' : 'Pause'}
                      </button>
                    )}

                    {confirmStop === printer.id ? (
                      <button
                        onClick={() => sendCommand(printer.id, 'stop')}
                        disabled={!!isLoading}
                        className="flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 bg-red-600 text-white hover:bg-red-500 rounded-md text-sm transition-colors disabled:opacity-50 animate-pulse"
                      >
                        <StopCircle size={14} />
                        {isLoading === 'stop' ? 'Stopping...' : 'Confirm Stop'}
                      </button>
                    ) : (
                      <button
                        onClick={() => setConfirmStop(printer.id)}
                        disabled={!!isLoading}
                        className="flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 bg-red-900 hover:bg-red-800 rounded-md text-sm transition-colors disabled:opacity-50"
                      >
                        <StopCircle size={14} />
                        Stop
                      </button>
                    )}
                  </div>
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
