import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { FileText, Download, ChevronLeft, ChevronRight } from 'lucide-react'
import { auditLogs } from '../../api'
import { PageHeader, SearchInput, Button, EmptyState, Select } from '../../components/ui'

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

function summarizeDetails(details: any) {
  if (!details || typeof details !== 'object') return null
  const parts = []
  if (details.name) parts.push(details.name)
  if (details.status) parts.push(`status: ${details.status}`)
  if (details.role) parts.push(`role: ${details.role}`)
  if (details.changes && typeof details.changes === 'object') {
    const keys = Object.keys(details.changes)
    if (keys.length > 0) parts.push(`changed: ${keys.join(', ')}`)
  }
  if (details.reason) parts.push(details.reason)
  if (details.ip) parts.push(`from ${details.ip}`)
  return parts.length > 0 ? parts.join(' \u2022 ') : null
}

export default function AuditLogs() {
  const [entityType, setEntityType] = useState('')
  const [action, setAction] = useState('')
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [limit, setLimit] = useState(50)
  const [offset, setOffset] = useState(0)
  const [expandedId, setExpandedId] = useState(null)
  const [searchText, setSearchText] = useState('')

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

  const allLogs = data?.logs || []
  const total = data?.total || 0

  // Client-side text search filter
  const logs = searchText
    ? allLogs.filter(log => {
        const q = searchText.toLowerCase()
        return (
          (log.action || '').toLowerCase().includes(q) ||
          (log.entity_type || '').toLowerCase().includes(q) ||
          (log.username || '').toLowerCase().includes(q) ||
          String(log.entity_id || '').toLowerCase().includes(q) ||
          JSON.stringify(log.details || '').toLowerCase().includes(q)
        )
      })
    : allLogs
  const page = Math.floor(offset / limit) + 1
  const totalPages = Math.ceil(total / limit)

  const resetPagination = () => setOffset(0)

  const exportCsv = () => {
    let url = '/api/export/audit-logs?'
    if (entityType) url += `entity_type=${entityType}&`
    if (action) url += `action=${action}&`
    fetch(url, { credentials: 'include' })
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
      <PageHeader icon={FileText} title="Audit Log">
        <span className="text-sm text-[var(--brand-text-muted)]">{total.toLocaleString()} entries</span>
      </PageHeader>

      {/* Filters */}
      <div className="flex flex-wrap gap-3 mb-4">
        <SearchInput
          value={searchText}
          onChange={e => setSearchText(e.target.value)}
          placeholder="Search logs..."
          className="w-48"
        />
        <Select value={entityType} onChange={e => { setEntityType(e.target.value); resetPagination() }} className="w-auto">
          <option value="">All Types</option>
          {ENTITY_TYPES.map(t => <option key={t} value={t}>{t.charAt(0).toUpperCase() + t.slice(1)}</option>)}
        </Select>
        <Select value={action} onChange={e => { setAction(e.target.value); resetPagination() }} className="w-auto">
          <option value="">All Actions</option>
          {ACTIONS.map(a => <option key={a} value={a}>{a.charAt(0).toUpperCase() + a.slice(1)}</option>)}
        </Select>
        <input type="date" value={dateFrom} onChange={e => { setDateFrom(e.target.value); resetPagination() }} className="bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-sm" placeholder="From" />
        <input type="date" value={dateTo} onChange={e => { setDateTo(e.target.value); resetPagination() }} className="bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-sm" placeholder="To" />
        <Select value={limit} onChange={e => { setLimit(Number(e.target.value)); resetPagination() }} className="w-auto">
          {PAGE_SIZES.map(s => <option key={s} value={s}>{s} / page</option>)}
        </Select>
        <Button variant="secondary" size="md" icon={Download} onClick={exportCsv} className="ml-auto">
          Export CSV
        </Button>
      </div>

      {/* Table */}
      <div className="bg-[var(--brand-card-bg)] border border-[var(--brand-card-border)] rounded-md overflow-hidden overflow-x-auto">
        <table className="w-full text-sm min-w-[700px]">
          <thead>
            <tr className="border-b border-[var(--brand-card-border)] text-[var(--brand-text-muted)] text-xs uppercase">
              <th className="text-left px-4 py-3 font-medium">Timestamp</th>
              <th className="text-left px-4 py-3 font-medium">User</th>
              <th className="text-left px-4 py-3 font-medium">Action</th>
              <th className="text-left px-4 py-3 font-medium">Type</th>
              <th className="text-left px-4 py-3 font-medium">ID</th>
              <th className="text-left px-4 py-3 font-medium">Details</th>
              <th className="text-left px-4 py-3 font-medium">IP</th>
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              <tr><td colSpan={7} className="text-center py-8 text-[var(--brand-text-muted)]">Loading...</td></tr>
            ) : logs.length === 0 ? (
              <tr><td colSpan={7}><EmptyState icon={FileText} title={searchText ? 'No matching entries' : 'No audit log entries'} /></td></tr>
            ) : logs.map(log => {
              const summary = summarizeDetails(log.details)
              return (
              <tr key={log.id} className="border-b border-[var(--brand-card-border)]/50 hover:bg-[var(--brand-input-bg)]/30 transition-colors">
                <td className="px-4 py-2.5 text-[var(--brand-text-secondary)] whitespace-nowrap font-mono text-xs">
                  {log.timestamp ? new Date(log.timestamp).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—'}
                </td>
                <td className="px-4 py-2.5 text-[var(--brand-text-secondary)] whitespace-nowrap">
                  {log.username || '—'}
                </td>
                <td className={`px-4 py-2.5 font-medium ${ACTION_COLORS[log.action] || 'text-[var(--brand-text-secondary)]'}`}>
                  {log.action || '—'}
                </td>
                <td className="px-4 py-2.5 text-[var(--brand-text-secondary)]">{log.entity_type || '—'}</td>
                <td className="px-4 py-2.5 text-[var(--brand-text-muted)] font-mono text-xs">{log.entity_id ?? '—'}</td>
                <td className="px-4 py-2.5 text-[var(--brand-text-secondary)] max-w-xs">
                  {log.details ? (
                    <button
                      onClick={() => setExpandedId(expandedId === log.id ? null : log.id)}
                      className="text-left hover:text-[var(--brand-text-primary)] transition-colors"
                    >
                      {expandedId === log.id ? (
                        <pre className="text-xs whitespace-pre-wrap font-mono">{JSON.stringify(log.details, null, 2)}</pre>
                      ) : (
                        <span className="truncate block max-w-xs">{summary || JSON.stringify(log.details)}</span>
                      )}
                    </button>
                  ) : '—'}
                </td>
                <td className="px-4 py-2.5 text-[var(--brand-text-muted)] whitespace-nowrap font-mono text-xs">{log.ip_address || '—'}</td>
              </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between mt-4">
          <Button
            variant="secondary"
            size="md"
            icon={ChevronLeft}
            onClick={() => setOffset(Math.max(0, offset - limit))}
            disabled={offset === 0}
          >
            Previous
          </Button>
          <span className="text-sm text-[var(--brand-text-muted)]">
            Page {page} of {totalPages}
          </span>
          <Button
            variant="secondary"
            size="md"
            icon={ChevronRight}
            iconPosition="right"
            onClick={() => setOffset(offset + limit)}
            disabled={offset + limit >= total}
          >
            Next
          </Button>
        </div>
      )}
    </div>
  )
}
