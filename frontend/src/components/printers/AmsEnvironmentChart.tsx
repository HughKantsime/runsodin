import { useState, useEffect, useRef, useMemo } from 'react'
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend } from 'recharts'
import { X } from 'lucide-react'
import type { AmsEnvironmentData } from '../../types'
import { getAmsEnvironment } from '../../api'

interface AmsEnvironmentChartProps {
  printerId: number
  onClose?: () => void
}

interface FormattedDataPoint {
  time: string
  temp: number | null
  humidity: number | null
  timestamp: number
}

export default function AmsEnvironmentChart({ printerId, onClose }: AmsEnvironmentChartProps) {
  const [units, setUnits] = useState<AmsEnvironmentData['units']>({})
  const [selectedUnit, setSelectedUnit] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [humidityUnit, setHumidityUnit] = useState<'level' | 'unknown'>('unknown')
  const generation = useRef(0)
  const [hours, setHours] = useState(24)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    generation.current += 1
    setUnits({})
    loadData()
    return () => { generation.current += 1 }
  }, [printerId, hours])

  const loadData = async () => {
    const requestGeneration = generation.current
    setLoading(true)
    setError(null)
    try {
      const result = await getAmsEnvironment(printerId, hours)
      if (requestGeneration !== generation.current) return
      const nextUnits = result?.units || {}
      setUnits(nextUnits)
      setHumidityUnit(result?.humidity_unit === 'level' ? 'level' : 'unknown')
      setSelectedUnit(previous => Object.prototype.hasOwnProperty.call(nextUnits, previous) ? previous : Object.keys(nextUnits).sort((a,b) => Number(a)-Number(b))[0] || '')
    } catch {
      if (requestGeneration !== generation.current) return
      setError('Could not load AMS environment data. Please try again.')
    } finally {
      if (requestGeneration === generation.current) setLoading(false)
    }
  }

  const data = useMemo<FormattedDataPoint[]>(() => (units[selectedUnit] || []).map(d => {
    const timestamp = d.time.trim()
    const date = new Date(/(?:Z|[+-]\d{2}:?\d{2})$/i.test(timestamp) ? timestamp : `${timestamp}Z`)
    return {
      timestamp: date.getTime(),
      time: date.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }),
      temp: d.temperature != null && Number.isFinite(d.temperature) ? Number(d.temperature.toFixed(1)) : null,
      humidity: d.humidity != null && Number.isFinite(d.humidity) ? Number(d.humidity.toFixed(1)) : null,
    }
  }).filter(d => Number.isFinite(d.timestamp)).sort((a,b) => a.timestamp-b.timestamp), [units, selectedUnit])
  const humidityLabel = humidityUnit === 'level' ? 'Humidity level (1–5)' : 'Humidity (reported value)'

  return (
    <div className="rounded-md p-4" style={{ backgroundColor: 'var(--brand-card-bg)', border: '1px solid var(--brand-sidebar-border)' }}>
      <div className="flex items-center justify-between mb-3">
        <h4 className="text-sm font-semibold" style={{ color: 'var(--brand-text-primary)' }}>AMS Environment</h4>
        <div className="flex items-center gap-2">
          {[6, 24, 48, 168].map(h => (
            <button
              key={h}
              onClick={() => setHours(h)}
              className={`px-2 py-1 rounded-md text-xs transition-colors ${hours === h ? 'bg-[var(--brand-primary)] text-white' : 'bg-[var(--brand-card-bg)] text-[var(--brand-text-secondary)] hover:bg-[var(--brand-card-border)]'}`}
            >
              {h < 48 ? `${h}h` : `${h / 24}d`}
            </button>
          ))}
          {onClose && (
            <button onClick={onClose} className="ml-2 text-[var(--brand-text-muted)] hover:text-[var(--brand-text-primary)] text-xs"><X size={12} /></button>
          )}
        </div>
      </div>

      {!loading && !error && Object.keys(units).length > 0 && (
        <label className="text-xs text-[var(--brand-text-secondary)]">AMS unit
          <select aria-label="AMS unit" value={selectedUnit} onChange={e => setSelectedUnit(e.target.value)} className="ml-2 bg-[var(--brand-card-bg)] border border-[var(--brand-card-border)] rounded-md px-2 py-1">
            {Object.keys(units).sort((a,b) => Number(a)-Number(b)).map(unit => <option key={unit} value={unit}>AMS {Number(unit)+1}</option>)}
          </select>
        </label>
      )}
      {loading ? (
        <div className="text-center py-8 text-[var(--brand-text-muted)] text-sm">Loading...</div>
      ) : error ? (
        <div><p role="alert">{error}</p><button onClick={loadData}>Retry</button></div>
      ) : data.length === 0 ? (
        <div className="text-center py-8 text-[var(--brand-text-muted)] text-sm">No AMS environment data available</div>
      ) : (
        <ResponsiveContainer width="100%" height={200}>
          <LineChart data={data}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--chart-grid)" />
            <XAxis dataKey="time" tick={{ fill: 'var(--chart-axis)', fontSize: 11 }} interval="preserveStartEnd" />
            <YAxis yAxisId="temp" tick={{ fill: '#F59E0B', fontSize: 11 }} domain={['auto', 'auto']} label={{ value: '°C', position: 'insideLeft', fill: '#F59E0B', fontSize: 11 }} />
            <YAxis yAxisId="humidity" orientation="right" tick={{ fill: '#3B82F6', fontSize: 11 }} domain={humidityUnit === 'level' ? [1, 5] : ['auto', 'auto']} label={{ value: humidityUnit === 'level' ? 'Level' : 'Value', position: 'insideRight', fill: '#3B82F6', fontSize: 11 }} />
            <Tooltip
              contentStyle={{ backgroundColor: 'var(--chart-tooltip-bg)', border: 'none', boxShadow: 'var(--chart-tooltip-shadow)', borderRadius: '6px', fontSize: '12px' }}
              labelStyle={{ color: 'var(--chart-axis)' }}
            />
            <Legend wrapperStyle={{ fontSize: '11px' }} />
            <Line yAxisId="temp" type="monotone" dataKey="temp" stroke="#F59E0B" strokeWidth={2} dot={false} name="Temperature (°C)" />
            <Line yAxisId="humidity" type="monotone" dataKey="humidity" stroke="#3B82F6" strokeWidth={2} dot={false} name={humidityLabel} />
          </LineChart>
        </ResponsiveContainer>
      )}
    </div>
  )
}
