import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { archives, printers as printersApi } from '../../api'
import { ClipboardList, Search, Download, Clock, ChevronLeft, ChevronRight } from 'lucide-react'

const STATUS_BADGES = {
  completed: 'bg-green-600/20 text-green-400',
  failed: 'bg-red-600/20 text-red-400',
  cancelled: 'bg-yellow-600/20 text-yellow-400',
}

function formatDuration(seconds) {
  if (!seconds) return '--'
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  return h > 0 ? `${h}h ${m}m` : `${m}m`
}

function formatDate(d) {
  if (!d) return '--'
  return new Date(d).toLocaleString([], { month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit' })
}

export default function PrintLog() {
  const [page, setPage] = useState(1)
  const [search, setSearch] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const [filterPrinter, setFilterPrinter] = useState('')
  const [filterStatus, setFilterStatus] = useState('')

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
        <ClipboardList className="text-print-400" size={24} />
        <div>
          <h1 className="text-xl md:text-2xl font-display font-bold">Print Log</h1>
          <p className="text-farm-500 text-sm mt-1">{total} entries</p>
        </div>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-2 mb-4">
        <div className="relative flex-1 min-w-[200px] max-w-xs">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-farm-500" />
          <input
            type="text"
            value={searchInput}
            onChange={e => setSearchInput(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') { setSearch(searchInput); setPage(1) } }}
            placeholder="Search prints..."
            className="w-full pl-9 pr-3 py-2 bg-farm-800 border border-farm-700 rounded-lg text-sm"
          />
        </div>
        <select
          value={filterPrinter}
          onChange={e => { setFilterPrinter(e.target.value); setPage(1) }}
          className="bg-farm-800 border border-farm-700 rounded-lg px-2 py-2 text-sm"
        >
          <option value="">All Printers</option>
          {(printerList || []).map(p => (
            <option key={p.id} value={p.id}>{p.nickname || p.name}</option>
          ))}
        </select>
        <select
          value={filterStatus}
          onChange={e => { setFilterStatus(e.target.value); setPage(1) }}
          className="bg-farm-800 border border-farm-700 rounded-lg px-2 py-2 text-sm"
        >
          <option value="">All Statuses</option>
          <option value="completed">Completed</option>
          <option value="failed">Failed</option>
          <option value="cancelled">Cancelled</option>
        </select>
        <button
          onClick={handleExport}
          className="flex items-center gap-1.5 px-3 py-2 bg-farm-800 border border-farm-700 rounded-lg text-sm hover:bg-farm-700 transition-colors"
        >
          <Download size={14} />
          Export CSV
        </button>
      </div>

      {/* Table */}
      {isLoading ? (
        <div className="text-center py-12 text-farm-500">Loading...</div>
      ) : items.length === 0 ? (
        <div className="text-center py-12 text-farm-500">
          <ClipboardList size={40} className="mx-auto mb-3 opacity-30" />
          <p>No print log entries</p>
        </div>
      ) : (
        <>
          <div className="overflow-x-auto rounded-lg border border-farm-800">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-farm-900 text-farm-400 text-xs">
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
                  <tr key={item.id} className="border-t border-farm-800 hover:bg-farm-800/50 transition-colors">
                    <td className="py-2 px-3 text-farm-400 text-xs whitespace-nowrap">{formatDate(item.completed_at)}</td>
                    <td className="py-2 px-3 font-medium truncate max-w-[200px]">{item.print_name}</td>
                    <td className="py-2 px-3 text-farm-400 hidden md:table-cell">{item.printer_display}</td>
                    <td className="py-2 px-3 text-farm-400 hidden lg:table-cell">{item.user_name || '--'}</td>
                    <td className="py-2 px-3">
                      <span className={`px-2 py-0.5 rounded text-xs ${STATUS_BADGES[item.status] || 'bg-farm-800 text-farm-400'}`}>
                        {item.status}
                      </span>
                    </td>
                    <td className="py-2 px-3 text-farm-400 hidden lg:table-cell">
                      <span className="flex items-center gap-1"><Clock size={12} /> {formatDuration(item.actual_duration_seconds)}</span>
                    </td>
                    <td className="py-2 px-3 text-farm-400 hidden lg:table-cell">
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
              <span className="text-xs text-farm-500">{total} results</span>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => setPage(p => Math.max(1, p - 1))}
                  disabled={page === 1}
                  className="p-1.5 rounded-lg bg-farm-800 text-farm-400 hover:bg-farm-700 disabled:opacity-30"
                >
                  <ChevronLeft size={16} />
                </button>
                <span className="text-sm text-farm-400">{page} / {totalPages}</span>
                <button
                  onClick={() => setPage(p => Math.min(totalPages, p + 1))}
                  disabled={page === totalPages}
                  className="p-1.5 rounded-lg bg-farm-800 text-farm-400 hover:bg-farm-700 disabled:opacity-30"
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
