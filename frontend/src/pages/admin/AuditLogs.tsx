import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { FileText, Download, ChevronLeft, ChevronRight } from 'lucide-react'
import { auditLogs, auth } from '../../api'
import { PageHeader, SearchInput, Button, EmptyState, Select } from '../../components/ui'
import toast from 'react-hot-toast'
import type { User } from '../../types'

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
  const queryClient = useQueryClient()
  const [entityType, setEntityType] = useState('')
  const [action, setAction] = useState('')
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [limit, setLimit] = useState(50)
  const [offset, setOffset] = useState(0)
  const [expandedId, setExpandedId] = useState(null)
  const [searchText, setSearchText] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [exporting, setExporting] = useState(false)
  const exportPending = useRef(false)
  const mounted = useRef(true)
  const { data: principal } = useQuery<User | null>({ queryKey: ['auth', 'me'], queryFn: auth.me, enabled: false })
  const ready = Number.isInteger(principal?.id) && (principal?.id ?? 0) > 0 && principal?.role === 'admin' && principal?.group_id == null
  const audience = `${principal?.id ?? ''}:${principal?.role ?? ''}:${principal?.group_id ?? ''}`
  const currentAudience = useRef(audience)
  currentAudience.current = audience
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedSearch(searchText), 250)
    return () => window.clearTimeout(timer)
  }, [searchText])

  const { data, isLoading, isError, isFetching, refetch } = useQuery({
    queryKey: ['audit-logs', audience, entityType, action, dateFrom, dateTo, debouncedSearch, limit, offset],
    enabled: ready,
    queryFn: () => auditLogs.list({
      limit, offset,
      entity_type: entityType || undefined,
      action: action || undefined,
      date_from: dateFrom || undefined,
      date_to: dateTo || undefined,
      q: debouncedSearch || undefined,
    }),
  })

  const logs = ready ? data?.logs || [] : []
  const total = data?.total || 0

  const page = Math.floor(offset / limit) + 1
  const totalPages = Math.ceil(total / limit)

  const resetPagination = () => setOffset(0)

  const exportCsv = async () => {
    if (!ready || exportPending.current) return
    exportPending.current = true
    setExporting(true)
    const expectedAudience = audience
    try {
      const csv = await auditLogs.exportCsv({ entity_type: entityType || undefined, action: action || undefined,
        date_from: dateFrom || undefined, date_to: dateTo || undefined, q: searchText || undefined })
      const latest = queryClient.getQueryData<User | null>(['auth', 'me'])
      const latestAudience = `${latest?.id ?? ''}:${latest?.role ?? ''}:${latest?.group_id ?? ''}`
      if (!mounted.current || currentAudience.current !== expectedAudience || latestAudience !== expectedAudience) return
      if (typeof csv !== 'string' || !csv) throw new Error('Export did not return CSV')
      const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }))
      try {
        const link = document.createElement('a')
        link.href = url
        link.download = `audit-logs-${new Date().toISOString().split('T')[0]}.csv`
        link.click()
      } finally { URL.revokeObjectURL(url) }
    } catch {
      if (mounted.current && currentAudience.current === expectedAudience) toast.error('Audit export failed. Please try again.')
    } finally {
      exportPending.current = false
      if (mounted.current) setExporting(false)
    }
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
          onChange={e => { setSearchText(e.target.value); resetPagination() }}
          maxLength={200}
          placeholder="Search logs..."
          className="w-48"
        />
        <Select aria-label="Entity type" value={entityType} onChange={e => { setEntityType(e.target.value); resetPagination() }} className="w-auto">
          <option value="">All Types</option>
          {ENTITY_TYPES.map(t => <option key={t} value={t}>{t.charAt(0).toUpperCase() + t.slice(1)}</option>)}
        </Select>
        <Select aria-label="Action" value={action} onChange={e => { setAction(e.target.value); resetPagination() }} className="w-auto">
          <option value="">All Actions</option>
          {ACTIONS.map(a => <option key={a} value={a}>{a.charAt(0).toUpperCase() + a.slice(1)}</option>)}
        </Select>
        <input aria-label="From date" type="date" value={dateFrom} onChange={e => { setDateFrom(e.target.value); resetPagination() }} className="bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-sm" placeholder="From" />
        <input aria-label="To date" type="date" value={dateTo} onChange={e => { setDateTo(e.target.value); resetPagination() }} className="bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-sm" placeholder="To" />
        <Select aria-label="Entries per page" value={limit} onChange={e => { setLimit(Number(e.target.value)); resetPagination() }} className="w-auto">
          {PAGE_SIZES.map(s => <option key={s} value={s}>{s} / page</option>)}
        </Select>
        <Button variant="secondary" size="md" icon={Download} onClick={exportCsv} disabled={!ready || exporting} className="ml-auto">
          {exporting ? 'Exporting...' : 'Export CSV'}
        </Button>
      </div>

      <p className="mb-3 text-xs text-[var(--brand-text-muted)]">CSV export includes up to 5,000 matching entries.</p>

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
            {principal && !ready ? (
              <tr><td colSpan={7} role="alert">Audit history requires platform administrator access.</td></tr>
            ) : isError ? (
              <tr><td colSpan={7} role="alert">Audit logs unavailable. <button type="button" onClick={() => { void refetch() }} disabled={isFetching}>Retry</button></td></tr>
            ) : isLoading ? (
              <tr><td colSpan={7} className="text-center py-8 text-[var(--brand-text-muted)]">Loading...</td></tr>
            ) : logs.length === 0 ? (
              <tr><td colSpan={7}><EmptyState icon={FileText} title={searchText ? 'No matching entries' : 'No audit log entries'} /></td></tr>
            ) : logs.map(log => {
              const summary = summarizeDetails(log.details)
              return (
              <tr key={log.id} className="border-b border-[var(--brand-card-border)]/50 hover:bg-[var(--brand-input-bg)]/30 transition-colors">
                <td className="px-4 py-2.5 text-[var(--brand-text-secondary)] whitespace-nowrap font-mono text-xs">
                  {log.timestamp ? new Date(log.timestamp).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' }) : 'Unknown'}
                </td>
                <td className="px-4 py-2.5 text-[var(--brand-text-secondary)] whitespace-nowrap">
                  {log.auth_kind === 'legacy_global_api_key' ? 'Service API key' : log.username || 'Unknown'}
                </td>
                <td className={`px-4 py-2.5 font-medium ${ACTION_COLORS[log.action] || 'text-[var(--brand-text-secondary)]'}`}>
                  {log.action || 'Unknown'}
                </td>
                <td className="px-4 py-2.5 text-[var(--brand-text-secondary)]">{log.entity_type || 'Unknown'}</td>
                <td className="px-4 py-2.5 text-[var(--brand-text-muted)] font-mono text-xs">{log.entity_id ?? 'Unknown'}</td>
                <td className="px-4 py-2.5 text-[var(--brand-text-secondary)] max-w-xs">
                  {log.details !== null && log.details !== undefined ? (
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
                  ) : 'Unknown'}
                </td>
                <td className="px-4 py-2.5 text-[var(--brand-text-muted)] whitespace-nowrap font-mono text-xs">{log.ip_address || 'Unknown'}</td>
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
