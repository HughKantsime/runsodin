import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { FileText, Download, ChevronLeft, ChevronRight } from 'lucide-react'
import { auditLogs } from '../api'

const ACTION_COLORS = {
  create: 'text-green-400',
  update: 'text-blue-400',
  delete: 'text-red-400',
  login: 'text-amber-400',
  schedule: 'text-purple-400',
  sync: 'text-cyan-400',
}

const ENTITY_TYPES = ['job', 'printer', 'spool', 'model', 'order', 'user', 'settings']
const ACTIONS = ['create', 'update', 'delete', 'login', 'schedule', 'sync']
const PAGE_SIZES = [25, 50, 100]

export default function AuditLogs() {
  const [entityType, setEntityType] = useState('')
  const [action, setAction] = useState('')
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [limit, setLimit] = useState(50)
  const [offset, setOffset] = useState(0)
  const [expandedId, setExpandedId] = useState(null)

  const { data, isLoading } = useQuery({
    queryKey: ['audit-logs', entityType, action, dateFrom, dateTo, limit, offset],
    queryFn: () => auditLogs.list({
      limit, offset,
      entity_type: entityType || undefined,
      action: action || undefined,
      date_from: dateFrom || undefined,
      date_to: dateTo || undefined,
    }),
  })

  const logs = data?.logs || []
  const total = data?.total || 0
  const page = Math.floor(offset / limit) + 1
  const totalPages = Math.ceil(total / limit)

  const resetPagination = () => setOffset(0)

  const exportCsv = () => {
    let url = '/api/export/audit-logs?'
    if (entityType) url += `entity_type=${entityType}&`
    if (action) url += `action=${action}&`
    const token = localStorage.getItem('token')
    const headers = {}
    if (token) headers['Authorization'] = 'Bearer ' + token
    const apiKey = import.meta.env.VITE_API_KEY
    if (apiKey) headers['X-API-Key'] = apiKey
    fetch(url, { headers })
      .then(r => r.blob())
      .then(blob => {
        const u = URL.createObjectURL(blob)
        const link = document.createElement('a')
        link.href = u
        link.download = `audit-logs-${new Date().toISOString().split('T')[0]}.csv`
        link.click()
        URL.revokeObjectURL(u)
      })
  }

  return (
    <div className="p-4 md:p-6 max-w-7xl mx-auto">
      {/* Header */}
      <div className="flex items-center gap-3 mb-6">
        <FileText size={24} className="text-print-400" />
        <h1 className="text-2xl font-display font-bold">Audit Log</h1>
        <span className="text-sm text-farm-500 ml-auto">{total.toLocaleString()} entries</span>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap gap-3 mb-4">
        <select value={entityType} onChange={e => { setEntityType(e.target.value); resetPagination() }} className="bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm">
          <option value="">All Types</option>
          {ENTITY_TYPES.map(t => <option key={t} value={t}>{t.charAt(0).toUpperCase() + t.slice(1)}</option>)}
        </select>
        <select value={action} onChange={e => { setAction(e.target.value); resetPagination() }} className="bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm">
          <option value="">All Actions</option>
          {ACTIONS.map(a => <option key={a} value={a}>{a.charAt(0).toUpperCase() + a.slice(1)}</option>)}
        </select>
        <input type="date" value={dateFrom} onChange={e => { setDateFrom(e.target.value); resetPagination() }} className="bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm" placeholder="From" />
        <input type="date" value={dateTo} onChange={e => { setDateTo(e.target.value); resetPagination() }} className="bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm" placeholder="To" />
        <select value={limit} onChange={e => { setLimit(Number(e.target.value)); resetPagination() }} className="bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm">
          {PAGE_SIZES.map(s => <option key={s} value={s}>{s} / page</option>)}
        </select>
        <button onClick={exportCsv} className="flex items-center gap-2 bg-farm-800 hover:bg-farm-700 border border-farm-700 rounded-lg px-3 py-2 text-sm text-farm-300 transition-colors ml-auto">
          <Download size={14} /> Export CSV
        </button>
      </div>

      {/* Table */}
      <div className="bg-farm-900 border border-farm-800 rounded-lg overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-farm-800 text-farm-500 text-xs uppercase">
              <th className="text-left px-4 py-3 font-medium">Timestamp</th>
              <th className="text-left px-4 py-3 font-medium">Action</th>
              <th className="text-left px-4 py-3 font-medium">Type</th>
              <th className="text-left px-4 py-3 font-medium">ID</th>
              <th className="text-left px-4 py-3 font-medium">Details</th>
              <th className="text-left px-4 py-3 font-medium">IP</th>
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              <tr><td colSpan={6} className="text-center py-8 text-farm-500">Loading...</td></tr>
            ) : logs.length === 0 ? (
              <tr><td colSpan={6} className="text-center py-8 text-farm-500">No audit log entries</td></tr>
            ) : logs.map(log => (
              <tr key={log.id} className="border-b border-farm-800/50 hover:bg-farm-800/30 transition-colors">
                <td className="px-4 py-2.5 text-farm-400 whitespace-nowrap">
                  {log.timestamp ? new Date(log.timestamp).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—'}
                </td>
                <td className={`px-4 py-2.5 font-medium ${ACTION_COLORS[log.action] || 'text-farm-400'}`}>
                  {log.action || '—'}
                </td>
                <td className="px-4 py-2.5 text-farm-400">{log.entity_type || '—'}</td>
                <td className="px-4 py-2.5 text-farm-500">{log.entity_id ?? '—'}</td>
                <td className="px-4 py-2.5 text-farm-300 max-w-xs">
                  {log.details ? (
                    <button
                      onClick={() => setExpandedId(expandedId === log.id ? null : log.id)}
                      className="text-left hover:text-farm-100 transition-colors"
                    >
                      {expandedId === log.id ? (
                        <pre className="text-xs whitespace-pre-wrap font-mono">{JSON.stringify(log.details, null, 2)}</pre>
                      ) : (
                        <span className="truncate block max-w-xs">{JSON.stringify(log.details)}</span>
                      )}
                    </button>
                  ) : '—'}
                </td>
                <td className="px-4 py-2.5 text-farm-600 whitespace-nowrap">{log.ip_address || '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between mt-4">
          <button
            onClick={() => setOffset(Math.max(0, offset - limit))}
            disabled={offset === 0}
            className="flex items-center gap-1 px-3 py-2 text-sm bg-farm-800 hover:bg-farm-700 border border-farm-700 rounded-lg disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            <ChevronLeft size={14} /> Previous
          </button>
          <span className="text-sm text-farm-500">
            Page {page} of {totalPages}
          </span>
          <button
            onClick={() => setOffset(offset + limit)}
            disabled={offset + limit >= total}
            className="flex items-center gap-1 px-3 py-2 text-sm bg-farm-800 hover:bg-farm-700 border border-farm-700 rounded-lg disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            Next <ChevronRight size={14} />
          </button>
        </div>
      )}
    </div>
  )
}
