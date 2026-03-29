import { useState, useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { archives, printers as printersApi } from '../../api'
import { ClipboardList, Search, Download, Clock, ChevronLeft, ChevronRight } from 'lucide-react'
import { formatDurationSecs as formatDuration, formatDate } from '../../utils/shared'

const STATUS_BADGES = {
  completed: 'bg-green-600/20 text-green-400',
  failed: 'bg-red-600/20 text-red-400',
  cancelled: 'bg-yellow-600/20 text-yellow-400',
}

export default function PrintLog() {
  const [searchParams, setSearchParams] = useSearchParams()

  const [page, setPage] = useState(() => {
    const p = parseInt(searchParams.get('page'), 10)
    return p > 0 ? p : 1
  })
  const [search, _setSearch] = useState(() => searchParams.get('q') || '')
  const [searchInput, setSearchInput] = useState(() => searchParams.get('q') || '')
  const [filterPrinter, _setFilterPrinter] = useState(() => searchParams.get('printer') || '')
  const [filterStatus, _setFilterStatus] = useState(() => searchParams.get('status') || '')

  const updateSearchParams = useCallback((updates) => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      Object.entries(updates).forEach(([key, value]) => {
        if (value && value !== '') {
          next.set(key, value)
        } else {
          next.delete(key)
        }
      })
      return next
    }, { replace: true })
  }, [setSearchParams])

  const setSearch = useCallback((value) => {
    _setSearch(value)
    updateSearchParams({ q: value })
  }, [updateSearchParams])

  const setFilterPrinter = useCallback((value) => {
    _setFilterPrinter(value)
    setPage(1)
    updateSearchParams({ printer: value, page: '' })
  }, [updateSearchParams])

  const setFilterStatus = useCallback((value) => {
    _setFilterStatus(value)
    setPage(1)
    updateSearchParams({ status: value, page: '' })
  }, [updateSearchParams])

  const { data: printerList } = useQuery({
    queryKey: ['printers-list'],
    queryFn: () => printersApi.list(),
  })

  const { data, isLoading } = useQuery({
    queryKey: ['print-log', page, search, filterPrinter, filterStatus],
    queryFn: () => archives.log({
      page,
      per_page: 50,
      search: search || undefined,
      printer_id: filterPrinter || undefined,
      status: filterStatus || undefined,
    }),
  })

  const handleExport = async () => {
    try {
      const csv = await archives.logExport({
        search: search || undefined,
        printer_id: filterPrinter || undefined,
        status: filterStatus || undefined,
      })
      const blob = new Blob([csv], { type: 'text/csv' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = 'print_log.csv'
      a.click()
      URL.revokeObjectURL(url)
    } catch { /* ignore */ }
  }

  const items = data?.items || []
  const total = data?.total || 0
  const totalPages = Math.ceil(total / 50) || 1

  return (
    <div className="p-4 md:p-6">
      {/* Header */}
      <div className="flex items-center gap-3 mb-4 md:mb-6">
        <ClipboardList className="text-[var(--brand-primary)]" size={24} />
        <div>
          <h1 className="text-xl md:text-2xl font-display font-bold">Print Log</h1>
          <p className="text-[var(--brand-text-muted)] text-sm mt-1">{total} entries</p>
        </div>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-2 mb-4">
        <div className="relative flex-1 min-w-[200px] max-w-xs">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-[var(--brand-text-muted)]" />
          <input
            type="text"
            value={searchInput}
            onChange={e => setSearchInput(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') { setSearch(searchInput); setPage(1) } }}
            placeholder="Search prints..."
            className="w-full pl-9 pr-3 py-2 bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md text-sm"
          />
        </div>
        <select
          value={filterPrinter}
          onChange={e => setFilterPrinter(e.target.value)}
          className="bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-2 py-2 text-sm"
        >
          <option value="">All Printers</option>
          {(printerList || []).map(p => (
            <option key={p.id} value={p.id}>{p.nickname || p.name}</option>
          ))}
        </select>
        <select
          value={filterStatus}
          onChange={e => setFilterStatus(e.target.value)}
          className="bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-2 py-2 text-sm"
        >
          <option value="">All Statuses</option>
          <option value="completed">Completed</option>
          <option value="failed">Failed</option>
          <option value="cancelled">Cancelled</option>
        </select>
        <button
          onClick={handleExport}
          className="flex items-center gap-1.5 px-3 py-2 bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md text-sm hover:bg-[var(--brand-card-border)] transition-colors"
        >
          <Download size={14} />
          Export CSV
        </button>
      </div>

      {/* Table */}
      {isLoading ? (
        <div className="text-center py-12 text-[var(--brand-text-muted)]">Loading...</div>
      ) : items.length === 0 ? (
        <div className="text-center py-12 text-[var(--brand-text-muted)]">
          <ClipboardList size={40} className="mx-auto mb-3 opacity-30" />
          <p>No print log entries</p>
        </div>
      ) : (
        <>
          <div className="overflow-x-auto rounded-md border border-[var(--brand-card-border)]">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-[var(--brand-card-bg)] text-[var(--brand-text-muted)] text-xs">
                  <th className="py-2 px-3 text-left">Date</th>
                  <th className="py-2 px-3 text-left">Name</th>
                  <th className="py-2 px-3 text-left hidden md:table-cell">Printer</th>
                  <th className="py-2 px-3 text-left hidden lg:table-cell">User</th>
                  <th className="py-2 px-3 text-left">Status</th>
                  <th className="py-2 px-3 text-left hidden lg:table-cell">Duration</th>
                  <th className="py-2 px-3 text-left hidden lg:table-cell">Filament</th>
                </tr>
              </thead>
              <tbody>
                {items.map(item => (
                  <tr key={item.id} className="border-t border-[var(--brand-card-border)] hover:bg-[var(--brand-input-bg)]/50 transition-colors">
                    <td className="py-2 px-3 text-[var(--brand-text-muted)] text-xs whitespace-nowrap font-mono">{formatDate(item.completed_at)}</td>
                    <td className="py-2 px-3 font-medium truncate max-w-[200px]">{item.print_name}</td>
                    <td className="py-2 px-3 text-[var(--brand-text-muted)] hidden md:table-cell">{item.printer_display}</td>
                    <td className="py-2 px-3 text-[var(--brand-text-muted)] hidden lg:table-cell">{item.user_name || '--'}</td>
                    <td className="py-2 px-3">
                      <span className={`px-2 py-0.5 rounded text-xs ${STATUS_BADGES[item.status] || 'bg-[var(--brand-input-bg)] text-[var(--brand-text-muted)]'}`}>
                        {item.status}
                      </span>
                    </td>
                    <td className="py-2 px-3 text-[var(--brand-text-muted)] hidden lg:table-cell">
                      <span className="flex items-center gap-1"><Clock size={12} /> {formatDuration(item.actual_duration_seconds)}</span>
                    </td>
                    <td className="py-2 px-3 text-[var(--brand-text-muted)] hidden lg:table-cell">
                      {item.filament_used_grams ? `${item.filament_used_grams.toFixed(1)}g` : '--'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Pagination */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between mt-4">
              <span className="text-xs text-[var(--brand-text-muted)]">{total} results</span>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => { const p = Math.max(1, page - 1); setPage(p); updateSearchParams({ page: p > 1 ? String(p) : '' }) }}
                  disabled={page === 1}
                  className="p-1.5 rounded-md bg-[var(--brand-input-bg)] text-[var(--brand-text-muted)] hover:bg-[var(--brand-card-border)] disabled:opacity-30"
                >
                  <ChevronLeft size={16} />
                </button>
                <span className="text-sm text-[var(--brand-text-muted)]">{page} / {totalPages}</span>
                <button
                  onClick={() => { const p = Math.min(totalPages, page + 1); setPage(p); updateSearchParams({ page: p > 1 ? String(p) : '' }) }}
                  disabled={page === totalPages}
                  className="p-1.5 rounded-md bg-[var(--brand-input-bg)] text-[var(--brand-text-muted)] hover:bg-[var(--brand-card-border)] disabled:opacity-30"
                >
                  <ChevronRight size={16} />
                </button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}
