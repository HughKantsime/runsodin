import { useState, useEffect, useRef, type FormEvent } from 'react'
import { X } from 'lucide-react'
import { canDo } from '../../permissions'
import { printerTelemetry } from '../../api'
import type { NozzleLifecycle } from '../../types'

interface NozzleStatusCardProps {
  printerId: number
  onClose?: () => void
}

interface InstallForm {
  nozzle_type: string
  nozzle_diameter: number
  notes: string
}

export default function NozzleStatusCard({ printerId, onClose }: NozzleStatusCardProps) {
  const [current, setCurrent] = useState<NozzleLifecycle | null>(null)
  const [history, setHistory] = useState<NozzleLifecycle[]>([])
  const [showHistory, setShowHistory] = useState(false)
  const [showInstall, setShowInstall] = useState(false)
  const [installForm, setInstallForm] = useState<InstallForm>({ nozzle_type: 'hardened_steel', nozzle_diameter: 0.4, notes: '' })
  const [error, setError] = useState<string | null>(null)
  const [loadFailed, setLoadFailed] = useState(false)
  const [pending, setPending] = useState(false)
  const mutationPending = useRef(false)
  const generation = useRef(0)
  const canManage = canDo('printers.edit')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    generation.current += 1
    setCurrent(null)
    setHistory([])
    setShowHistory(false)
    setShowInstall(false)
    setInstallForm({ nozzle_type: 'hardened_steel', nozzle_diameter: 0.4, notes: '' })
    loadNozzle()
    return () => { generation.current += 1 }
  }, [printerId])

  const loadNozzle = async () => {
    const requestGeneration = generation.current
    setLoading(true)
    setLoadFailed(false)
    setError(null)
    try {
      const n = await printerTelemetry.nozzle(printerId)
      if (requestGeneration !== generation.current) return
      setCurrent(n)
    } catch {
      if (requestGeneration !== generation.current) return
      setLoadFailed(true)
      setError('Could not load nozzle. Please try again.')
    } finally {
      if (requestGeneration === generation.current) setLoading(false)
    }
  }

  const loadHistory = async () => {
    const requestGeneration = generation.current
    setError(null)
    try {
      const h = await printerTelemetry.nozzleHistory(printerId)
      if (requestGeneration !== generation.current) return
      setHistory(h || [])
      setShowHistory(true)
    } catch {
      if (requestGeneration !== generation.current) return
      setError('Could not load nozzle history. Please try again.')
    }
  }

  const handleInstall = async (e: FormEvent) => {
    e.preventDefault()
    if (!canManage || mutationPending.current) return
    const requestGeneration = generation.current
    mutationPending.current = true
    setPending(true)
    setError(null)
    try {
      await printerTelemetry.installNozzle(printerId, installForm)
      if (requestGeneration !== generation.current) return
      setShowInstall(false)
      setInstallForm({ nozzle_type: 'hardened_steel', nozzle_diameter: 0.4, notes: '' })
      await loadNozzle()
    } catch {
      if (requestGeneration !== generation.current) return
      setError('Could not install nozzle. Please try again.')
    } finally {
      mutationPending.current = false
      setPending(false)
    }
  }

  const handleRetire = async () => {
    if (!canManage || mutationPending.current || !current || !confirm('Retire this nozzle?')) return
    const requestGeneration = generation.current
    mutationPending.current = true
    setPending(true)
    setError(null)
    try {
      await printerTelemetry.retireNozzle(printerId, current.id)
      if (requestGeneration !== generation.current) return
      await loadNozzle()
    } catch {
      if (requestGeneration !== generation.current) return
      setError('Could not retire nozzle. Please try again.')
    } finally {
      mutationPending.current = false
      setPending(false)
    }
  }

  const formatDate = (d: string | null) => d ? new Date(d).toLocaleDateString() : 'Unknown'

  return (
    <div className="rounded-md p-4" style={{ backgroundColor: 'var(--brand-card-bg)', border: '1px solid var(--brand-sidebar-border)' }}>
      <div className="flex items-center justify-between mb-3">
        <h4 className="text-sm font-semibold" style={{ color: 'var(--brand-text-primary)' }}>Nozzle Lifecycle</h4>
        <div className="flex items-center gap-2">
          {onClose && (
            <button onClick={onClose} className="text-[var(--brand-text-muted)] hover:text-[var(--brand-text-primary)] text-xs"><X size={12} /></button>
          )}
        </div>
      </div>

      {error && <p role="alert" className="text-sm text-red-400">{error}</p>}
      {loading ? (
        <div className="text-center py-4 text-[var(--brand-text-muted)] text-sm">Loading...</div>
      ) : loadFailed ? (
        <button onClick={loadNozzle}>Retry</button>
      ) : current ? (
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3 text-sm">
            <div>
              <span className="text-[var(--brand-text-muted)] text-xs">Type</span>
              <div style={{ color: 'var(--brand-text-primary)' }}>{current.nozzle_type || 'Unknown'}</div>
            </div>
            <div>
              <span className="text-[var(--brand-text-muted)] text-xs">Diameter</span>
              <div className="font-mono" style={{ color: 'var(--brand-text-primary)' }}>{current.nozzle_diameter ? `${current.nozzle_diameter}mm` : 'Unknown'}</div>
            </div>
            <div>
              <span className="text-[var(--brand-text-muted)] text-xs">Print Hours</span>
              <div className="font-mono" style={{ color: 'var(--brand-text-primary)' }}>{current.print_hours_accumulated == null ? 'Unknown' : `${current.print_hours_accumulated.toFixed(1)}h`}</div>
            </div>
            <div>
              <span className="text-[var(--brand-text-muted)] text-xs">Print Count</span>
              <div className="font-mono" style={{ color: 'var(--brand-text-primary)' }}>{current.print_count ?? 'Unknown'}</div>
            </div>
            <div>
              <span className="text-[var(--brand-text-muted)] text-xs">Installed</span>
              <div style={{ color: 'var(--brand-text-primary)' }}>{formatDate(current.installed_at)}</div>
            </div>
          </div>
          <div className="flex gap-2 pt-1">
            {canManage && <button disabled={pending} onClick={handleRetire} className="px-3 py-1 bg-red-600/20 text-red-400 rounded-md text-xs hover:bg-red-600/30">
              Retire
            </button>}
            {canManage && <button disabled={pending} onClick={() => setShowInstall(true)} className="px-3 py-1 bg-[var(--brand-primary)]/20 text-[var(--brand-primary)] rounded-md text-xs hover:bg-[var(--brand-primary)]/30">
              Replace
            </button>}
            <button onClick={loadHistory} className="px-3 py-1 bg-[var(--brand-card-border)] text-[var(--brand-text-secondary)] rounded-md text-xs hover:bg-[var(--brand-card-bg)]">
              History
            </button>
          </div>
        </div>
      ) : (
        <div className="text-center py-4">
          <p className="text-[var(--brand-text-muted)] text-sm mb-2">No nozzle tracked</p>
          {canManage && <button disabled={pending} onClick={() => setShowInstall(true)} className="px-3 py-1 bg-[var(--brand-primary)] text-white rounded-md text-xs hover:bg-[var(--brand-primary-hover)]">
            Track Nozzle
          </button>}
        </div>
      )}

      {/* Install modal */}
      {showInstall && canManage && (
        <div className="mt-3 pt-3 border-t border-[var(--brand-card-border)]">
          <form onSubmit={handleInstall} className="space-y-2">
            <div className="grid grid-cols-2 gap-2">
              <div>
                <label className="text-xs text-[var(--brand-text-muted)]">Type</label>
                <select value={installForm.nozzle_type} onChange={e => setInstallForm(f => ({ ...f, nozzle_type: e.target.value }))}
                  className="w-full bg-[var(--brand-card-bg)] border border-[var(--brand-card-border)] rounded-md px-2 py-1 text-sm" style={{ color: 'var(--brand-text-primary)' }}>
                  <option value="hardened_steel">Hardened Steel</option>
                  <option value="stainless_steel">Stainless Steel</option>
                  <option value="brass">Brass</option>
                  <option value="copper">Copper Alloy</option>
                  <option value="ruby">Ruby</option>
                </select>
              </div>
              <div>
                <label className="text-xs text-[var(--brand-text-muted)]">Diameter (mm)</label>
                <select value={installForm.nozzle_diameter} onChange={e => setInstallForm(f => ({ ...f, nozzle_diameter: parseFloat(e.target.value) }))}
                  className="w-full bg-[var(--brand-card-bg)] border border-[var(--brand-card-border)] rounded-md px-2 py-1 text-sm" style={{ color: 'var(--brand-text-primary)' }}>
                  <option value={0.2}>0.2</option>
                  <option value={0.4}>0.4</option>
                  <option value={0.6}>0.6</option>
                  <option value={0.8}>0.8</option>
                </select>
              </div>
            </div>
            <input type="text" placeholder="Notes (optional)" value={installForm.notes}
              onChange={e => setInstallForm(f => ({ ...f, notes: e.target.value }))}
              className="w-full bg-[var(--brand-card-bg)] border border-[var(--brand-card-border)] rounded-md px-2 py-1 text-sm" style={{ color: 'var(--brand-text-primary)' }} />
            <div className="flex gap-2">
              <button disabled={pending} type="submit" className="px-3 py-1 bg-[var(--brand-primary)] text-white rounded-md text-xs hover:bg-[var(--brand-primary-hover)]">Install</button>
              <button disabled={pending} type="button" onClick={() => setShowInstall(false)} className="px-3 py-1 bg-[var(--brand-card-border)] text-[var(--brand-text-secondary)] rounded-md text-xs hover:bg-[var(--brand-card-bg)]">Cancel</button>
            </div>
          </form>
        </div>
      )}

      {/* History */}
      {showHistory && history.length > 0 && (
        <div className="mt-3 pt-3 border-t border-[var(--brand-card-border)]">
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs font-semibold text-[var(--brand-text-secondary)]">Nozzle History</span>
            <button onClick={() => setShowHistory(false)} className="text-[var(--brand-text-muted)] hover:text-[var(--brand-text-primary)] text-xs"><X size={12} /></button>
          </div>
          <div className="space-y-1 max-h-40 overflow-y-auto">
            {history.map(n => (
              <div key={n.id} className="flex justify-between text-xs py-1 border-b border-[var(--brand-card-border)]">
                <span style={{ color: 'var(--brand-text-primary)' }}>
                  {n.nozzle_type || '?'} {n.nozzle_diameter}mm
                </span>
                <span className="text-[var(--brand-text-muted)]">
                  {n.print_hours_accumulated == null ? 'Unknown hours' : `${n.print_hours_accumulated.toFixed(1)}h`} / {n.print_count == null ? 'Unknown print count' : `${n.print_count} prints`}
                </span>
                <span className="text-[var(--brand-text-muted)]">
                  {formatDate(n.installed_at)} to {n.removed_at ? formatDate(n.removed_at) : 'active'}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
