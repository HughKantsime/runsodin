import { useState, useEffect, useRef } from 'react'
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts'
import { Zap, Pencil } from 'lucide-react'
import { getEnergyRate, setEnergyRate as updateEnergyRate } from '../../api'

const CHART_TOOLTIP = { backgroundColor: 'var(--chart-tooltip-bg)', border: 'none', borderRadius: '6px', boxShadow: 'var(--chart-tooltip-shadow)', color: 'var(--brand-text-primary)' }

export default function EnergyWidget({ jobs }: { jobs?: any[] }) {
  const [rate, setRate] = useState<number | null>(null)
  const [editing, setEditing] = useState(false)
  const [tempRate, setTempRate] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const requestVersion = useRef(0)
  const validRate = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0
  useEffect(() => {
    loadRate()
    return () => { requestVersion.current += 1 }
  }, [])
  const loadRate = async () => {
    const version = ++requestVersion.current
    setLoading(true); setError(null)
    try {
      const data = await getEnergyRate()
      if (version !== requestVersion.current) return
      if (!validRate(data?.energy_cost_per_kwh)) throw new Error('Invalid rate response')
      setRate(data.energy_cost_per_kwh); setTempRate(String(data.energy_cost_per_kwh))
    } catch {
      if (version === requestVersion.current) setError('Energy rate unavailable')
    } finally {
      if (version === requestVersion.current) setLoading(false)
    }
  }
  const saveRate = async () => {
    if (saving) return
    const value = tempRate.trim() === '' ? NaN : Number(tempRate)
    if (!validRate(value)) { setError('Enter a finite nonnegative energy rate'); return }
    const version = ++requestVersion.current
    setSaving(true); setError(null)
    try {
      const data = await updateEnergyRate(value)
      if (version !== requestVersion.current) return
      if (!validRate(data?.energy_cost_per_kwh)) throw new Error('Invalid rate response')
      setRate(data.energy_cost_per_kwh); setTempRate(String(data.energy_cost_per_kwh)); setEditing(false)
    } catch {
      if (version === requestVersion.current) setError('Could not save energy rate')
    } finally {
      if (version === requestVersion.current) setSaving(false)
    }
  }

  // Aggregate energy data from jobs that have kWh data
  const jobsWithEnergy = (jobs || []).filter(j => j.energy_kwh > 0)
  const totalKwh = jobsWithEnergy.reduce((sum, j) => sum + (j.energy_kwh || 0), 0)
  // A job's cost is the energy_cost recorded when it finished (rate at that time);
  // only jobs without a recorded cost are valued at the current rate.
  const recordedCost = (j: any): number | null => (typeof j.energy_cost === 'number' && Number.isFinite(j.energy_cost) ? j.energy_cost : null)
  const jobCost = (j: any): number | null => recordedCost(j) ?? (rate === null ? null : (j.energy_kwh || 0) * rate)
  const costs = jobsWithEnergy.map(jobCost)
  const totalCost = costs.some(c => c === null) ? null : costs.reduce((sum: number, c) => sum + (c as number), 0)

  // Group by the UTC day the print ended (/print-jobs ended_at, stored as UTC).
  const endDay = (value: unknown): string | null => {
    if (typeof value !== 'string' || !value) return null
    const iso = value.includes('T') ? value : value.replace(' ', 'T')
    const parsed = new Date(/(Z|[+-]\d\d:?\d\d)$/.test(iso) ? iso : `${iso}Z`)
    return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString().slice(0, 10)
  }
  const byDay: Record<string, { kwh: number; cost: number | null }> = {}
  jobsWithEnergy.forEach(j => {
    const day = endDay(j.ended_at) ?? 'Unknown'
    const bucket = byDay[day] ?? (byDay[day] = { kwh: 0, cost: 0 })
    bucket.kwh += j.energy_kwh || 0
    const cost = jobCost(j)
    bucket.cost = bucket.cost === null || cost === null ? null : bucket.cost + cost
  })
  const label = (day: string) => day === 'Unknown' ? day : new Date(`${day}T00:00:00Z`).toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' })
  const chartData = Object.entries(byDay)
    .sort(([a], [b]) => (a === 'Unknown' ? 1 : b === 'Unknown' ? -1 : a.localeCompare(b)))
    .map(([day, v]) => ({ day: label(day), kwh: parseFloat(v.kwh.toFixed(2)), cost: v.cost === null ? null : parseFloat(v.cost.toFixed(2)) }))

  return (
    <div className="rounded-md p-5" style={{ backgroundColor: 'var(--brand-card-bg)', border: '1px solid var(--brand-card-border)' }}>
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <Zap size={18} className="text-yellow-400" />
          <h3 className="font-semibold" style={{ color: 'var(--brand-text-primary)' }}>Energy Consumption</h3>
        </div>
        <div className="flex items-center gap-2 text-xs">
          {editing ? (
            <>
              <span className="text-[var(--brand-text-muted)]">$/kWh:</span>
              <input
                type="number"
                step="0.01"
                min="0"
                disabled={saving}
                aria-label="Energy rate per kWh"
                value={tempRate}
                onChange={(e) => setTempRate(e.target.value)}
                className="w-20 bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-2 py-1 text-sm"
                autoFocus
                onKeyDown={(e) => e.key === 'Enter' && saveRate()}
              />
              <button disabled={saving} onClick={saveRate} className="text-green-400 hover:text-green-300">{saving ? 'Saving...' : 'Save'}</button>
              <button disabled={saving} onClick={() => { setEditing(false); setError(null); setTempRate(String(rate)) }} className="text-[var(--brand-text-muted)] hover:text-[var(--brand-text-secondary)]">Cancel</button>
            </>
          ) : rate === null ? (
            <span>{loading ? 'Loading energy rate...' : 'Rate unavailable'}</span>
          ) : (
            <button onClick={() => setEditing(true)} className="text-[var(--brand-text-muted)] hover:text-[var(--brand-text-primary)] transition-colors">
              ${rate}/kWh <Pencil size={10} className="inline ml-1" />
            </button>
          )}
        </div>
      </div>

      {error && <div role="alert" className="mb-3 text-sm text-red-400">
        {error}
        {rate === null && <button disabled={loading} onClick={loadRate} className="ml-2 underline">Retry</button>}
      </div>}
      <div className="grid grid-cols-3 gap-4 mb-4">
        <div className="text-center">
          <div className="text-2xl font-bold text-yellow-400">{totalKwh.toFixed(1)}</div>
          <div className="text-xs text-[var(--brand-text-muted)]">Total kWh</div>
        </div>
        <div className="text-center">
          <div className="text-2xl font-bold text-green-400">{totalCost === null ? '--' : `$${totalCost.toFixed(2)}`}</div>
          <div className="text-xs text-[var(--brand-text-muted)]">Total Cost</div>
        </div>
        <div className="text-center">
          <div className="text-2xl font-bold text-[var(--brand-text-secondary)]">{jobsWithEnergy.length}</div>
          <div className="text-xs text-[var(--brand-text-muted)]">Jobs Tracked</div>
        </div>
      </div>

      {chartData.length > 0 ? (
        <ResponsiveContainer width="100%" height={150}>
          <BarChart data={chartData}>
            <CartesianGrid strokeDasharray="3 3" stroke="#1A1D25" />
            <XAxis dataKey="day" tick={{ fill: '#3D4559', fontSize: 11 }} />
            <YAxis tick={{ fill: '#3D4559', fontSize: 11 }} />
            <Tooltip
              contentStyle={CHART_TOOLTIP}
              formatter={(val, name) => [name === 'kwh' ? `${val} kWh` : `$${val}`, name === 'kwh' ? 'Energy' : 'Cost']}
            />
            <Bar dataKey="kwh" fill="#F59E0B" radius={[4, 4, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      ) : (
        <div className="text-center py-6 text-[var(--brand-text-muted)] text-sm">
          No energy data yet. Connect a smart plug to track per-job power usage.
        </div>
      )}
    </div>
  )
}
