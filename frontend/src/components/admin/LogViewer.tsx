import { useState, useEffect, useRef } from 'react'
import { adminLogs } from '../../api'

const LOG_SOURCES = [
  { id: 'backend', label: 'Backend' },
  { id: 'mqtt', label: 'MQTT' },
  { id: 'moonraker', label: 'Moonraker' },
  { id: 'prusalink', label: 'PrusaLink' },
  { id: 'elegoo', label: 'Elegoo' },
  { id: 'anycubic_lan', label: 'Anycubic LAN' },
  { id: 'vision', label: 'Vision AI' },
  { id: 'go2rtc', label: 'go2rtc' },
  { id: 'timelapse', label: 'Timelapse' },
  { id: 'reports', label: 'Reports' },
  { id: 'supervisord', label: 'Supervisord' },
]

const LEVELS = ['ALL', 'DEBUG', 'INFO', 'WARNING', 'ERROR']

const LEVEL_COLORS = {
  ERROR: 'text-red-400',
  WARNING: 'text-yellow-400',
  INFO: 'text-blue-400',
  DEBUG: 'text-[var(--brand-text-muted)]',
}

function detectLevel(line) {
  if (line.includes(' ERROR ') || line.includes('[ERROR]')) return 'ERROR'
  if (line.includes(' WARNING ') || line.includes('[WARNING]')) return 'WARNING'
  if (line.includes(' INFO ') || line.includes('[INFO]')) return 'INFO'
  if (line.includes(' DEBUG ') || line.includes('[DEBUG]')) return 'DEBUG'
  return null
}

function highlight(line: string, keyword: string) {
  if (!keyword) return line
  const parts = []
  const folded = line.toLowerCase(), term = keyword.toLowerCase()
  let from = 0, match = folded.indexOf(term)
  while (match !== -1) {
    parts.push(line.slice(from, match))
    parts.push(<mark key={match} className="bg-yellow-300 text-black">{line.slice(match, match + keyword.length)}</mark>)
    from = match + keyword.length
    match = folded.indexOf(term, from)
  }
  parts.push(line.slice(from))
  return parts
}

export default function LogViewer() {
  const [source, setSource] = useState('backend')
  const [level, setLevel] = useState('ALL')
  const [search, setSearch] = useState('')
  const [lines, setLines] = useState<string[]>([])
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [pageCursor, setPageCursor] = useState<string | null>(null)
  const [history, setHistory] = useState<(string | null)[]>([])
  const [appliedSearch, setAppliedSearch] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const requestRef = useRef(0)
  const [streaming, setStreaming] = useState(false)
  const [autoScroll, setAutoScroll] = useState(true)
  const containerRef = useRef(null)
  const esRef = useRef(null)

  // Initial load
  useEffect(() => {
    loadLogs()
    return () => { requestRef.current++; stopStream() }
  }, [source, level])

  const loadLogs = async (cursor: string | null = null, previous: (string | null)[] = []) => {
    const request = ++requestRef.current
    stopStream()
    setLoading(true)
    setError('')
    try {
      const args = [source, 500, level === 'ALL' ? null : level, search || null] as const
      const data = cursor ? await adminLogs.get(...args, cursor) : await adminLogs.get(...args)
      if (request !== requestRef.current) return
      setLines(data?.lines || [])
      setNextCursor(data?.next_cursor || null)
      setPageCursor(data?.page_cursor || cursor)
      setHistory(previous)
      setAppliedSearch(search)
    } catch (failure) {
      if (request === requestRef.current) setError(`Failed to load logs${failure instanceof Error ? ': ' + failure.message : ''}`)
    } finally {
      if (request === requestRef.current) setLoading(false)
    }
  }

  const startStream = () => {
    requestRef.current++
    setLoading(false)
    setError('')
    setNextCursor(null)
    setHistory([])
    setPageCursor(null)
    setAppliedSearch(search)
    stopStream()
    const url = adminLogs.streamUrl(source, level === 'ALL' ? null : level, search || null)
    const es = new EventSource(url, { withCredentials: true })
    es.onmessage = (e) => {
      if (esRef.current !== es) return
      try {
        const line = JSON.parse(e.data)
        if (typeof line !== 'string') return
        setLines(prev => [...prev.slice(-4999), line])
      } catch {}
    }
    es.onerror = () => { if (esRef.current !== es) return; es.close(); esRef.current = null; setStreaming(false) }
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
          aria-label="Log source"
          value={source}
          onChange={(e) => setSource(e.target.value)}
          className="bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-2 py-1.5 text-sm"
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
                level === l ? 'bg-[var(--brand-primary)] text-white' : 'bg-[var(--brand-input-bg)] text-[var(--brand-text-secondary)] hover:bg-[var(--brand-input-bg)]'
              }`}
            >
              {l}
            </button>
          ))}
        </div>

        {/* Search */}
        <input aria-label="Search logs"
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && loadLogs()}
          placeholder="Search..."
          className="bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-2 py-1.5 text-sm w-40"
        />

        {/* Actions */}
        <button onClick={() => loadLogs()} className="px-2 py-1.5 rounded-md text-xs bg-[var(--brand-input-bg)] text-[var(--brand-text-secondary)] hover:bg-[var(--brand-input-bg)]">
          Refresh
        </button>
        <button
          onClick={() => streaming ? stopStream() : startStream()}
          className={`px-2 py-1.5 rounded-md text-xs ${streaming ? 'bg-red-600/20 text-red-400' : 'bg-green-600/20 text-green-400'}`}
        >
          {streaming ? 'Stop Stream' : 'Live Stream'}
        </button>
        <label className="flex items-center gap-1 text-xs text-[var(--brand-text-secondary)] ml-auto">
          <input type="checkbox" checked={autoScroll} onChange={(e) => setAutoScroll(e.target.checked)} className="rounded" />
          Auto-scroll
        </label>
        <button onClick={() => setLines([])} className="px-2 py-1.5 rounded-md text-xs bg-[var(--brand-input-bg)] text-[var(--brand-text-muted)] hover:bg-[var(--brand-input-bg)]">
          Clear
        </button>
      </div>

      {error && <p role="alert">{error}</p>}
      <div className="flex items-center gap-2 text-sm">
        <button disabled={loading || streaming || search !== appliedSearch || history.length === 0}
          onClick={() => loadLogs(history[history.length - 1], history.slice(0, -1))}>Newer logs</button>
        <span>Page {history.length + 1}</span>
        <button disabled={loading || streaming || search !== appliedSearch || !nextCursor}
          onClick={() => nextCursor && loadLogs(nextCursor, [...history, pageCursor])}>Older logs</button>
        {loading && <span role="status">Loading logs…</span>}
      </div>
      {/* Log output */}
      <div
        ref={containerRef}
        className="bg-black rounded-md border border-[var(--brand-card-border)] p-3 font-mono text-xs leading-5 overflow-auto"
        style={{ height: '500px' }}
      >
        {lines.length === 0 ? (
          <span className="text-[var(--brand-text-muted)]">No log lines</span>
        ) : (
          lines.map((line, i) => {
            const lvl = detectLevel(line)
            return (
              <div key={i} className={`whitespace-pre-wrap break-all ${lvl ? LEVEL_COLORS[lvl] : 'text-[var(--brand-text-secondary)]'}`}>
                {highlight(line, appliedSearch)}
              </div>
            )
          })
        )}
      </div>
    </div>
  )
}
