import { useState, useEffect } from 'react'
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts'
import { Zap, Pencil } from 'lucide-react'
import { getEnergyRate, setEnergyRate as updateEnergyRate } from '../../api'

const CHART_TOOLTIP = { backgroundColor: 'var(--chart-tooltip-bg)', border: 'none', borderRadius: '6px', boxShadow: 'var(--chart-tooltip-shadow)', color: 'var(--brand-text-primary)' }

export default function EnergyWidget({ jobs }: { jobs?: any[] }) {
  const [rate, setRate] = useState(0.12)
  const [editing, setEditing] = useState(false)
  const [tempRate, setTempRate] = useState('0.12')

  useEffect(() => {
    loadRate()
  }, [])

  const loadRate = async () => {
    try {
      const data = await getEnergyRate()
      if (data?.energy_cost_per_kwh) {
        setRate(data.energy_cost_per_kwh)
        setTempRate(String(data.energy_cost_per_kwh))
      }
    } catch (e) {}
  }

  const saveRate = async () => {
    const val = parseFloat(tempRate)
    if (isNaN(val) || val < 0) return
    try {
      await updateEnergyRate(val)
      setRate(val)
      setEditing(false)
    } catch (e) {
      console.error('Failed to save energy rate:', e)
    }
  }

  // Aggregate energy data from jobs that have kWh data
  const jobsWithEnergy = (jobs || []).filter(j => j.energy_kwh > 0)
  const totalKwh = jobsWithEnergy.reduce((sum, j) => sum + (j.energy_kwh || 0), 0)
  const totalCost = totalKwh * rate

  // Group by day for chart
  const byDay = {}
  jobsWithEnergy.forEach(j => {
    const day = j.completed_at ? new Date(j.completed_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) : 'Unknown'
    byDay[day] = (byDay[day] || 0) + (j.energy_kwh || 0)
  })
  const chartData = Object.entries(byDay).map(([day, kwh]) => ({ day, kwh: parseFloat(kwh.toFixed(2)), cost: parseFloat((kwh * rate).toFixed(2)) }))

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
                value={tempRate}
                onChange={(e) => setTempRate(e.target.value)}
                className="w-20 bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-2 py-1 text-sm"
                autoFocus
                onKeyDown={(e) => e.key === 'Enter' && saveRate()}
              />
              <button onClick={saveRate} className="text-green-400 hover:text-green-300">Save</button>
              <button onClick={() => setEditing(false)} className="text-[var(--brand-text-muted)] hover:text-[var(--brand-text-secondary)]">Cancel</button>
            </>
          ) : (
            <button onClick={() => setEditing(true)} className="text-[var(--brand-text-muted)] hover:text-[var(--brand-text-primary)] transition-colors">
              ${rate}/kWh <Pencil size={10} className="inline ml-1" />
            </button>
          )}
        </div>
      </div>

      <div className="grid grid-cols-3 gap-4 mb-4">
        <div className="text-center">
          <div className="text-2xl font-bold text-yellow-400">{totalKwh.toFixed(1)}</div>
          <div className="text-xs text-[var(--brand-text-muted)]">Total kWh</div>
        </div>
        <div className="text-center">
          <div className="text-2xl font-bold text-green-400">${totalCost.toFixed(2)}</div>
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
