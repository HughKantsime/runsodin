import { useState, useEffect } from 'react'
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend } from 'recharts'
import { printerTelemetry } from '../api'

export default function PrinterTelemetryChart({ printerId, onClose }) {
  const [data, setData] = useState([])
  const [hours, setHours] = useState(24)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    loadData()
  }, [printerId, hours])

  const loadData = async () => {
    setLoading(true)
    try {
      const result = await printerTelemetry.get(printerId, hours)
      const formatted = (result || []).map(d => ({
        ...d,
        time: new Date(d.recorded_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
        bed: d.bed_temp != null ? parseFloat(d.bed_temp.toFixed(1)) : null,
        nozzle: d.nozzle_temp != null ? parseFloat(d.nozzle_temp.toFixed(1)) : null,
        fan: d.fan_speed != null ? d.fan_speed : null,
      }))
      setData(formatted)
    } catch (err) {
      console.error('Failed to load telemetry data:', err)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="rounded-lg p-4" style={{ backgroundColor: 'var(--brand-card-bg)', border: '1px solid var(--brand-sidebar-border)' }}>
      <div className="flex items-center justify-between mb-3">
        <h4 className="text-sm font-semibold" style={{ color: 'var(--brand-text-primary)' }}>Print Telemetry</h4>
        <div className="flex items-center gap-2">
          {[6, 24, 48, 168].map(h => (
            <button
              key={h}
              onClick={() => setHours(h)}
              className={`px-2 py-1 rounded text-xs transition-colors ${hours === h ? 'bg-print-600 text-white' : 'bg-farm-800 text-farm-400 hover:bg-farm-700'}`}
            >
              {h < 48 ? `${h}h` : `${h / 24}d`}
            </button>
          ))}
          {onClose && (
            <button onClick={onClose} className="ml-2 text-farm-500 hover:text-farm-300 text-xs">✕</button>
          )}
        </div>
      </div>

      {loading ? (
        <div className="text-center py-8 text-farm-500 text-sm">Loading...</div>
      ) : data.length === 0 ? (
        <div className="text-center py-8 text-farm-500 text-sm">No telemetry data — data is recorded during prints</div>
      ) : (
        <ResponsiveContainer width="100%" height={220}>
          <LineChart data={data}>
            <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
            <XAxis dataKey="time" tick={{ fill: '#9CA3AF', fontSize: 10 }} interval="preserveStartEnd" />
            <YAxis yAxisId="temp" tick={{ fill: '#EF4444', fontSize: 10 }} domain={['auto', 'auto']} label={{ value: '°C', position: 'insideLeft', fill: '#EF4444', fontSize: 10 }} />
            <YAxis yAxisId="fan" orientation="right" tick={{ fill: '#22C55E', fontSize: 10 }} domain={[0, 'auto']} label={{ value: 'RPM', position: 'insideRight', fill: '#22C55E', fontSize: 10 }} />
            <Tooltip
              contentStyle={{ backgroundColor: '#1F2937', border: '1px solid #374151', borderRadius: '8px', fontSize: '12px' }}
              labelStyle={{ color: '#9CA3AF' }}
            />
            <Legend wrapperStyle={{ fontSize: '11px' }} />
            <Line yAxisId="temp" type="monotone" dataKey="bed" stroke="#F59E0B" strokeWidth={2} dot={false} name="Bed (°C)" />
            <Line yAxisId="temp" type="monotone" dataKey="nozzle" stroke="#EF4444" strokeWidth={2} dot={false} name="Nozzle (°C)" />
            <Line yAxisId="fan" type="monotone" dataKey="fan" stroke="#22C55E" strokeWidth={1.5} dot={false} name="Fan Speed" />
          </LineChart>
        </ResponsiveContainer>
      )}
    </div>
  )
}
