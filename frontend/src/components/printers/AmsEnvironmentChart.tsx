import { useState, useEffect } from 'react'
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend } from 'recharts'
import { X } from 'lucide-react'
import { getAmsEnvironment } from '../../api'

interface AmsEnvironmentChartProps {
  printerId: number
  onClose?: () => void
}

interface FormattedDataPoint {
  time: string
  temp: number | null
  humidity: number | null
  [key: string]: any // TODO: type this properly
}

export default function AmsEnvironmentChart({ printerId, onClose }: AmsEnvironmentChartProps) {
  const [data, setData] = useState<FormattedDataPoint[]>([])
  const [hours, setHours] = useState(24)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    loadData()
  }, [printerId, hours])

  const loadData = async () => {
    setLoading(true)
    try {
      const result = await getAmsEnvironment(printerId, hours)
      const units = result?.units || {}
      const allPoints = Object.values(units).flat() as any[]
      const formatted = allPoints.map(d => ({
        ...d,
        time: new Date(d.time).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
        temp: d.temperature != null ? parseFloat(d.temperature.toFixed(1)) : null,
        humidity: d.humidity != null ? parseFloat(d.humidity.toFixed(1)) : null,
      }))
      setData(formatted)
    } catch (err) {
      console.error('Failed to load AMS environment data:', err)
    } finally {
      setLoading(false)
    }
  }

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

      {loading ? (
        <div className="text-center py-8 text-[var(--brand-text-muted)] text-sm">Loading...</div>
      ) : data.length === 0 ? (
        <div className="text-center py-8 text-[var(--brand-text-muted)] text-sm">No AMS environment data available</div>
      ) : (
        <ResponsiveContainer width="100%" height={200}>
          <LineChart data={data}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--chart-grid)" />
            <XAxis dataKey="time" tick={{ fill: 'var(--chart-axis)', fontSize: 11 }} interval="preserveStartEnd" />
            <YAxis yAxisId="temp" tick={{ fill: '#F59E0B', fontSize: 11 }} domain={['auto', 'auto']} label={{ value: '°C', position: 'insideLeft', fill: '#F59E0B', fontSize: 11 }} />
            <YAxis yAxisId="humidity" orientation="right" tick={{ fill: '#3B82F6', fontSize: 11 }} domain={[0, 100]} label={{ value: '%', position: 'insideRight', fill: '#3B82F6', fontSize: 11 }} />
            <Tooltip
              contentStyle={{ backgroundColor: 'var(--chart-tooltip-bg)', border: 'none', boxShadow: 'var(--chart-tooltip-shadow)', borderRadius: '6px', fontSize: '12px' }}
              labelStyle={{ color: 'var(--chart-axis)' }}
            />
            <Legend wrapperStyle={{ fontSize: '11px' }} />
            <Line yAxisId="temp" type="monotone" dataKey="temp" stroke="#F59E0B" strokeWidth={2} dot={false} name="Temperature (°C)" />
            <Line yAxisId="humidity" type="monotone" dataKey="humidity" stroke="#3B82F6" strokeWidth={2} dot={false} name="Humidity (%)" />
          </LineChart>
        </ResponsiveContainer>
      )}
    </div>
  )
}
