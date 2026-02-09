import { useState, useEffect } from 'react'
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts'
import { Zap } from 'lucide-react'
import { getEnergyRate, setEnergyRate as updateEnergyRate } from '../api'

export default function EnergyWidget({ jobs }) {
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
    <div className="rounded-lg p-5" style={{ backgroundColor: 'var(--brand-card-bg)', border: '1px solid var(--brand-sidebar-border)' }}>
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <Zap size={18} className="text-yellow-400" />
          <h3 className="font-semibold" style={{ color: 'var(--brand-text-primary)' }}>Energy Consumption</h3>
        </div>
        <div className="flex items-center gap-2 text-xs">
          {editing ? (
            <>
              <span className="text-farm-400">$/kWh:</span>
              <input
                type="number"
                step="0.01"
                value={tempRate}
                onChange={(e) => setTempRate(e.target.value)}
                className="w-20 bg-farm-800 border border-farm-700 rounded px-2 py-1 text-sm"
                autoFocus
                onKeyDown={(e) => e.key === 'Enter' && saveRate()}
              />
              <button onClick={saveRate} className="text-green-400 hover:text-green-300">Save</button>
              <button onClick={() => setEditing(false)} className="text-farm-500 hover:text-farm-300">Cancel</button>
            </>
          ) : (
            <button onClick={() => setEditing(true)} className="text-farm-400 hover:text-farm-200 transition-colors">
              ${rate}/kWh ✎
            </button>
          )}
        </div>
      </div>

      <div className="grid grid-cols-3 gap-4 mb-4">
        <div className="text-center">
          <div className="text-2xl font-bold text-yellow-400">{totalKwh.toFixed(1)}</div>
          <div className="text-xs text-farm-500">Total kWh</div>
        </div>
        <div className="text-center">
          <div className="text-2xl font-bold text-green-400">${totalCost.toFixed(2)}</div>
          <div className="text-xs text-farm-500">Total Cost</div>
        </div>
        <div className="text-center">
          <div className="text-2xl font-bold text-farm-300">{jobsWithEnergy.length}</div>
          <div className="text-xs text-farm-500">Jobs Tracked</div>
        </div>
      </div>

      {chartData.length > 0 ? (
        <ResponsiveContainer width="100%" height={150}>
          <BarChart data={chartData}>
            <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
            <XAxis dataKey="day" tick={{ fill: '#9CA3AF', fontSize: 10 }} />
            <YAxis tick={{ fill: '#9CA3AF', fontSize: 10 }} />
            <Tooltip
              contentStyle={{ backgroundColor: '#1F2937', border: '1px solid #374151', borderRadius: '8px', fontSize: '12px' }}
              formatter={(val, name) => [name === 'kwh' ? `${val} kWh` : `$${val}`, name === 'kwh' ? 'Energy' : 'Cost']}
            />
            <Bar dataKey="kwh" fill="#F59E0B" radius={[4, 4, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      ) : (
        <div className="text-center py-6 text-farm-500 text-sm">
          No energy data yet. Connect a smart plug to track per-job power usage.
        </div>
      )}
    </div>
  )
}
