import { useState, useEffect } from 'react'
import { printerTelemetry } from '../../api'

export default function NozzleStatusCard({ printerId, onClose }) {
  const [current, setCurrent] = useState(null)
  const [history, setHistory] = useState([])
  const [showHistory, setShowHistory] = useState(false)
  const [showInstall, setShowInstall] = useState(false)
  const [installForm, setInstallForm] = useState({ nozzle_type: 'hardened_steel', nozzle_diameter: 0.4, notes: '' })
  const [loading, setLoading] = useState(true)

  useEffect(() => { loadNozzle() }, [printerId])

  const loadNozzle = async () => {
    setLoading(true)
    try {
      const n = await printerTelemetry.nozzle(printerId)
      setCurrent(n)
    } catch {
      setCurrent(null)
    } finally {
      setLoading(false)
    }
  }

  const loadHistory = async () => {
    try {
      const h = await printerTelemetry.nozzleHistory(printerId)
      setHistory(h || [])
      setShowHistory(true)
    } catch (err) {
      console.error('Failed to load nozzle history:', err)
    }
  }

  const handleInstall = async (e) => {
    e.preventDefault()
    try {
      await printerTelemetry.installNozzle(printerId, installForm)
      setShowInstall(false)
      setInstallForm({ nozzle_type: 'hardened_steel', nozzle_diameter: 0.4, notes: '' })
      loadNozzle()
    } catch (err) {
      console.error('Failed to install nozzle:', err)
    }
  }

  const handleRetire = async () => {
    if (!current || !confirm('Retire this nozzle?')) return
    try {
      await printerTelemetry.retireNozzle(printerId, current.id)
      loadNozzle()
    } catch (err) {
      console.error('Failed to retire nozzle:', err)
    }
  }

  const formatDate = (d) => d ? new Date(d).toLocaleDateString() : '—'

  return (
    <div className="rounded-lg p-4" style={{ backgroundColor: 'var(--brand-card-bg)', border: '1px solid var(--brand-sidebar-border)' }}>
      <div className="flex items-center justify-between mb-3">
        <h4 className="text-sm font-semibold" style={{ color: 'var(--brand-text-primary)' }}>Nozzle Lifecycle</h4>
        <div className="flex items-center gap-2">
          {onClose && (
            <button onClick={onClose} className="text-farm-500 hover:text-farm-300 text-xs">✕</button>
          )}
        </div>
      </div>

      {loading ? (
        <div className="text-center py-4 text-farm-500 text-sm">Loading...</div>
      ) : current ? (
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3 text-sm">
            <div>
              <span className="text-farm-500 text-xs">Type</span>
              <div style={{ color: 'var(--brand-text-primary)' }}>{current.nozzle_type || '—'}</div>
            </div>
            <div>
              <span className="text-farm-500 text-xs">Diameter</span>
              <div style={{ color: 'var(--brand-text-primary)' }}>{current.nozzle_diameter ? `${current.nozzle_diameter}mm` : '—'}</div>
            </div>
            <div>
              <span className="text-farm-500 text-xs">Print Hours</span>
              <div style={{ color: 'var(--brand-text-primary)' }}>{current.print_hours_accumulated?.toFixed(1) || '0'}h</div>
            </div>
            <div>
              <span className="text-farm-500 text-xs">Print Count</span>
              <div style={{ color: 'var(--brand-text-primary)' }}>{current.print_count || 0}</div>
            </div>
            <div>
              <span className="text-farm-500 text-xs">Installed</span>
              <div style={{ color: 'var(--brand-text-primary)' }}>{formatDate(current.installed_at)}</div>
            </div>
          </div>
          <div className="flex gap-2 pt-1">
            <button onClick={handleRetire} className="px-3 py-1 bg-red-600/20 text-red-400 rounded-lg text-xs hover:bg-red-600/30">
              Retire
            </button>
            <button onClick={() => setShowInstall(true)} className="px-3 py-1 bg-print-600/20 text-print-400 rounded-lg text-xs hover:bg-print-600/30">
              Replace
            </button>
            <button onClick={loadHistory} className="px-3 py-1 bg-farm-700 text-farm-300 rounded-lg text-xs hover:bg-farm-600">
              History
            </button>
          </div>
        </div>
      ) : (
        <div className="text-center py-4">
          <p className="text-farm-500 text-sm mb-2">No nozzle tracked</p>
          <button onClick={() => setShowInstall(true)} className="px-3 py-1 bg-print-600 text-white rounded-lg text-xs hover:bg-print-500">
            Track Nozzle
          </button>
        </div>
      )}

      {/* Install modal */}
      {showInstall && (
        <div className="mt-3 pt-3 border-t border-farm-700">
          <form onSubmit={handleInstall} className="space-y-2">
            <div className="grid grid-cols-2 gap-2">
              <div>
                <label className="text-xs text-farm-500">Type</label>
                <select value={installForm.nozzle_type} onChange={e => setInstallForm(f => ({ ...f, nozzle_type: e.target.value }))}
                  className="w-full bg-farm-800 border border-farm-600 rounded-lg px-2 py-1 text-sm" style={{ color: 'var(--brand-text-primary)' }}>
                  <option value="hardened_steel">Hardened Steel</option>
                  <option value="stainless_steel">Stainless Steel</option>
                  <option value="brass">Brass</option>
                  <option value="copper">Copper Alloy</option>
                  <option value="ruby">Ruby</option>
                </select>
              </div>
              <div>
                <label className="text-xs text-farm-500">Diameter (mm)</label>
                <select value={installForm.nozzle_diameter} onChange={e => setInstallForm(f => ({ ...f, nozzle_diameter: parseFloat(e.target.value) }))}
                  className="w-full bg-farm-800 border border-farm-600 rounded-lg px-2 py-1 text-sm" style={{ color: 'var(--brand-text-primary)' }}>
                  <option value={0.2}>0.2</option>
                  <option value={0.4}>0.4</option>
                  <option value={0.6}>0.6</option>
                  <option value={0.8}>0.8</option>
                </select>
              </div>
            </div>
            <input type="text" placeholder="Notes (optional)" value={installForm.notes}
              onChange={e => setInstallForm(f => ({ ...f, notes: e.target.value }))}
              className="w-full bg-farm-800 border border-farm-600 rounded-lg px-2 py-1 text-sm" style={{ color: 'var(--brand-text-primary)' }} />
            <div className="flex gap-2">
              <button type="submit" className="px-3 py-1 bg-print-600 text-white rounded-lg text-xs hover:bg-print-500">Install</button>
              <button type="button" onClick={() => setShowInstall(false)} className="px-3 py-1 bg-farm-700 text-farm-300 rounded-lg text-xs hover:bg-farm-600">Cancel</button>
            </div>
          </form>
        </div>
      )}

      {/* History */}
      {showHistory && history.length > 0 && (
        <div className="mt-3 pt-3 border-t border-farm-700">
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs font-semibold text-farm-400">Nozzle History</span>
            <button onClick={() => setShowHistory(false)} className="text-farm-500 hover:text-farm-300 text-xs">✕</button>
          </div>
          <div className="space-y-1 max-h-40 overflow-y-auto">
            {history.map(n => (
              <div key={n.id} className="flex justify-between text-xs py-1 border-b border-farm-800">
                <span style={{ color: 'var(--brand-text-primary)' }}>
                  {n.nozzle_type || '?'} {n.nozzle_diameter}mm
                </span>
                <span className="text-farm-500">
                  {n.print_hours_accumulated?.toFixed(1)}h / {n.print_count} prints
                </span>
                <span className="text-farm-600">
                  {formatDate(n.installed_at)} — {n.removed_at ? formatDate(n.removed_at) : 'active'}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
