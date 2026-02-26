import { useState, useEffect, useRef } from 'react'
import { adminLogs } from '../../api'

const LOG_SOURCES = [
  { id: 'backend', label: 'Backend' },
  { id: 'mqtt', label: 'MQTT' },
  { id: 'moonraker', label: 'Moonraker' },
  { id: 'prusalink', label: 'PrusaLink' },
  { id: 'elegoo', label: 'Elegoo' },
  { id: 'vision', label: 'Vision AI' },
  { id: 'go2rtc', label: 'go2rtc' },
  { id: 'timelapse', label: 'Timelapse' },
  { id: 'reports', label: 'Reports' },
]

const LEVELS = ['ALL', 'DEBUG', 'INFO', 'WARNING', 'ERROR']

const LEVEL_COLORS = {
  ERROR: 'text-red-400',
  WARNING: 'text-yellow-400',
  INFO: 'text-blue-400',
  DEBUG: 'text-farm-500',
}

function detectLevel(line) {
  if (line.includes(' ERROR ') || line.includes('[ERROR]')) return 'ERROR'
  if (line.includes(' WARNING ') || line.includes('[WARNING]')) return 'WARNING'
  if (line.includes(' INFO ') || line.includes('[INFO]')) return 'INFO'
  if (line.includes(' DEBUG ') || line.includes('[DEBUG]')) return 'DEBUG'
  return null
}

export default function LogViewer() {
  const [source, setSource] = useState('backend')
  const [level, setLevel] = useState('ALL')
  const [search, setSearch] = useState('')
  const [lines, setLines] = useState([])
  const [streaming, setStreaming] = useState(false)
  const [autoScroll, setAutoScroll] = useState(true)
  const containerRef = useRef(null)
  const esRef = useRef(null)

  // Initial load
  useEffect(() => {
    loadLogs()
    return () => stopStream()
  }, [source])

  const loadLogs = async () => {
    stopStream()
    try {
      const data = await adminLogs.get(source, 500, level === 'ALL' ? null : level, search || null)
      setLines(data?.lines || [])
    } catch {
      setLines(['Failed to load logs'])
    }
  }

  const startStream = () => {
    stopStream()
    const url = adminLogs.streamUrl(source, level === 'ALL' ? null : level, search || null)
    const es = new EventSource(url, { withCredentials: true })
    es.onmessage = (e) => {
      try {
        const line = JSON.parse(e.data)
        setLines(prev => [...prev.slice(-4999), line])
      } catch {}
    }
    es.onerror = () => { es.close(); setStreaming(false) }
    esRef.current = es
    setStreaming(true)
  }

  const stopStream = () => {
    if (esRef.current) { esRef.current.close(); esRef.current = null }
    setStreaming(false)
  }

  // Auto-scroll
  useEffect(() => {
    if (autoScroll && containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight
    }
  }, [lines, autoScroll])

  return (
    <div className="space-y-3">
      {/* Controls */}
      <div className="flex flex-wrap items-center gap-2">
        {/* Source selector */}
        <select
          value={source}
          onChange={(e) => setSource(e.target.value)}
          className="bg-farm-800 border border-farm-700 rounded-lg px-2 py-1.5 text-sm"
        >
          {LOG_SOURCES.map(s => (
            <option key={s.id} value={s.id}>{s.label}</option>
          ))}
        </select>

        {/* Level filter */}
        <div className="flex gap-1">
          {LEVELS.map(l => (
            <button
              key={l}
              onClick={() => setLevel(l)}
              className={`px-2 py-1 rounded text-xs transition-colors ${
                level === l ? 'bg-print-600 text-white' : 'bg-farm-800 text-farm-400 hover:bg-farm-700'
              }`}
            >
              {l}
            </button>
          ))}
        </div>

        {/* Search */}
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && loadLogs()}
          placeholder="Search..."
          className="bg-farm-800 border border-farm-700 rounded-lg px-2 py-1.5 text-sm w-40"
        />

        {/* Actions */}
        <button onClick={loadLogs} className="px-2 py-1.5 rounded-lg text-xs bg-farm-800 text-farm-400 hover:bg-farm-700">
          Refresh
        </button>
        <button
          onClick={() => streaming ? stopStream() : startStream()}
          className={`px-2 py-1.5 rounded-lg text-xs ${streaming ? 'bg-red-600/20 text-red-400' : 'bg-green-600/20 text-green-400'}`}
        >
          {streaming ? 'Stop Stream' : 'Live Stream'}
        </button>
        <label className="flex items-center gap-1 text-xs text-farm-400 ml-auto">
          <input type="checkbox" checked={autoScroll} onChange={(e) => setAutoScroll(e.target.checked)} className="rounded" />
          Auto-scroll
        </label>
        <button onClick={() => setLines([])} className="px-2 py-1.5 rounded-lg text-xs bg-farm-800 text-farm-500 hover:bg-farm-700">
          Clear
        </button>
      </div>

      {/* Log output */}
      <div
        ref={containerRef}
        className="bg-black rounded-lg border border-farm-800 p-3 font-mono text-xs leading-5 overflow-auto"
        style={{ height: '500px' }}
      >
        {lines.length === 0 ? (
          <span className="text-farm-600">No log lines</span>
        ) : (
          lines.map((line, i) => {
            const lvl = detectLevel(line)
            return (
              <div key={i} className={`whitespace-pre-wrap break-all ${lvl ? LEVEL_COLORS[lvl] : 'text-farm-300'}`}>
                {line}
              </div>
            )
          })
        )}
      </div>
    </div>
  )
}
