import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { StopCircle, Pause, Play } from 'lucide-react'
import toast from 'react-hot-toast'
import { fetchAPI } from '../../api'
import { canDo, getCurrentUser } from '../../permissions'
import type { Printer, PrintControlAction } from '../../types/printer'
import { isOnline, isActivePrintState, isPausedState } from '../../utils/shared'
import { activeControl, useControlDeadlines, controlLabel, pendingMessage, unavailableMessage, unprovenHelp, type ControlReceipt } from '../../utils/printControl'

export function mayControlPrint() {
  const user = getCurrentUser()
  return !!user && ['operator', 'admin'].includes(user.role) && canDo('printers.edit')
}

function useControlState() {
  const [printers, setPrinters] = useState<Printer[]>([])
  const [loading, setLoading] = useState<Record<string, boolean>>({})
  const [confirmStop, setConfirmStop] = useState<number | null>(null)
  const [listError, setListError] = useState(false)
  const [outcome, setOutcome] = useState<{ printerId: number; text: string } | null>(null)
  const busy = useRef(new Map<string, symbol>())
  const lastStops = useRef(new Map<number, { sentAt: number; inFlight: boolean }>())
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
        for (const [id, stop] of lastStops.current) {
          if (!stop.inFlight) lastStops.current.delete(id)
        }
        setListError(false)
        setPrinters(data.filter((p: Printer) => p.pending_control || (isOnline(p) && isActivePrintState(p.gcode_state))))
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

  useControlDeadlines(printers)

  const sendCommand = async (printerId: number, action: PrintControlAction) => {
    const target = printers.find(p => p.id === printerId)
    const key = target?.api_type === 'moonraker' ? printerId + action : String(printerId)
    const principal = getCurrentUser()
    if (!target || !mayControlPrint() || !isOnline(target) || !isActivePrintState(target.gcode_state) ||
        target.unavailable_controls?.includes(action) || listError || busy.current.has(key)) return
    const request = Symbol()
    busy.current.set(key, request)
    latest.current.set(printerId, request)
    const identity = JSON.stringify(principal)
    const stillCurrent = () => mounted.current && JSON.stringify(getCurrentUser()) === identity
    setLoading(prev => ({ ...prev, [printerId + action]: true }))
    const release = () => {
      if (busy.current.get(key) !== request) return
      busy.current.delete(key)
      if (stillCurrent()) setLoading(prev => ({ ...prev, [printerId + action]: false }))
    }
    const repeatableStop = action === 'stop' && target?.api_type === 'moonraker'
    const stop = { sentAt: Date.now(), inFlight: true }
    if (repeatableStop) {
      lastStops.current.set(printerId, stop)
      setConfirmStop(null)
    }
    const timer = repeatableStop ? setTimeout(release, 3000) : undefined

    try {
      const receipt = await fetchAPI<ControlReceipt>(`/printers/${printerId}/${action}`, { method: 'POST' })
      if (!stillCurrent()) return
      if (latest.current.get(printerId) !== request) return
      if (receipt?.outcome) {
        setPrinters(rows => rows.map(p => p.id === printerId ? { ...p, pending_control: receipt.pending_control || null } : p))
      }
      let message = receipt?.success === true ? receipt.message || 'Command accepted; printer confirmation pending' : 'Command not confirmed'
      if (receipt?.unproven && action === 'stop') message += ' The printer unwinds filament for up to a few minutes before it is idle.'
      setOutcome({ printerId, text: message })
    } catch (error) {
      if (!stillCurrent() || latest.current.get(printerId) !== request) return
      setOutcome({ printerId, text: target?.api_type === 'moonraker' && error instanceof Error ? error.message : 'Command not confirmed' })
      toast.error('Printer command failed')
    } finally {
      clearTimeout(timer)
      stop.inFlight = false
      release()
      if (action === 'stop' && !repeatableStop && stillCurrent()) setConfirmStop(null)
    }
  }

  return { printers, loading, confirmStop, setConfirmStop, listError, outcome, busy, lastStops, sendCommand }
}

const ControlContext = createContext<ReturnType<typeof useControlState> | null>(null)
export const usePrintControls = () => useContext(ControlContext)

export function PrintControlsProvider({ children }: { children: ReactNode }) {
  const state = useControlState()
  return <ControlContext.Provider value={state}>{children}</ControlContext.Provider>
}

export function PrintControls({ printerId, inline = false }: { printerId: number; inline?: boolean }) {
  const controls = usePrintControls()
  const printer = controls?.printers.find(p => p.id === printerId)
  if (!controls || !printer || !mayControlPrint()) return null
  const { loading, confirmStop, setConfirmStop, busy, lastStops, sendCommand, outcome } = controls
  const isPaused = isPausedState(printer.gcode_state)
  const isLoading = (action: PrintControlAction) => loading[printer.id + action]
  const pending = activeControl(printer)
  const lastStop = lastStops.current.get(printer.id)?.sentAt ?? (pending?.action === 'stop' ? Date.parse(pending.sent_at) : undefined)
  const repeatedStop = lastStop !== undefined
    ? `Stop was already sent ${Math.max(0, Math.floor((Date.now() - lastStop) / 1000))} seconds ago. Send it again?`
    : null
  const disabled = (action: PrintControlAction) => (action !== 'stop' && pending?.action === action) ||
    (printer.api_type === 'moonraker' ? isLoading(action) : busy.current.has(String(printer.id)))
  const unavailable = (action: PrintControlAction) => printer.unavailable_controls?.includes(action)
  const toggle = isPaused ? 'resume' : 'pause'

  return <>
    {pending && <p>{pendingMessage(printer)}</p>}
    {isOnline(printer) && isActivePrintState(printer.gcode_state) && <>
    <div className="flex gap-2">
      {unavailable(toggle) ? <p>{unavailableMessage(toggle)}</p> : isPaused ? (
        <button
          onClick={() => sendCommand(printer.id, 'resume')}
          disabled={disabled('resume')}
          className="flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 bg-green-700 text-white hover:bg-green-800 rounded-md text-sm transition-colors disabled:opacity-50"
        >
          <Play size={14} />
          {isLoading('resume') ? 'Resuming...' : controlLabel(printer, 'resume')}
        </button>
      ) : (
        <button
          onClick={() => sendCommand(printer.id, 'pause')}
          disabled={disabled('pause')}
          className="flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 bg-amber-700 text-white hover:bg-amber-800 rounded-md text-sm transition-colors disabled:opacity-50"
        >
          <Pause size={14} />
          {isLoading('pause') ? 'Pausing...' : controlLabel(printer, 'pause')}
        </button>
      )}

      {unavailable('stop') ? <p>{unavailableMessage('stop')}</p> : confirmStop === printer.id ? (
        <button
          onClick={() => sendCommand(printer.id, 'stop')}
          disabled={disabled('stop')}
          className="flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 bg-red-700 text-white hover:bg-red-800 rounded-md text-sm transition-colors disabled:opacity-50"
        >
          <StopCircle size={14} />
          {isLoading('stop') ? 'Stopping...' : repeatedStop || 'Confirm ' + controlLabel(printer, 'stop')}
        </button>
      ) : (
        <button
          onClick={() => setConfirmStop(printer.id)}
          disabled={disabled('stop')}
          className="flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 bg-red-700 text-white hover:bg-red-800 rounded-md text-sm transition-colors disabled:opacity-50"
        >
          <StopCircle size={14} />
          {isLoading('stop') && printer.api_type === 'moonraker' ? 'Sending...' : controlLabel(printer, 'stop')}
        </button>
      )}
    </div>
    {(printer.unproven_controls?.includes(toggle) || printer.unproven_controls?.includes('stop')) && <p>{unprovenHelp}</p>}
    </>}
    {inline && outcome?.printerId === printerId && <p role="status">{outcome.text}</p>}
  </>
}
