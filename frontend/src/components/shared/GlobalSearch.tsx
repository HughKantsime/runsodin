import { useState, useEffect, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { Search, X, Package, ListTodo, Printer, Database } from 'lucide-react'
import { search } from '../../api'
import { useLicense } from '../../LicenseContext'
import { canAccessPage } from '../../permissions'

interface SearchResultItem {
  id: number
  name: string
  qr_code?: string
  status?: string
}

interface SearchResults {
  orders: SearchResultItem[]
  products: SearchResultItem[]
  models: SearchResultItem[]
  jobs: SearchResultItem[]
  spools: SearchResultItem[]
  printers: SearchResultItem[]
}

interface HighlightProps {
  text: string
  query: string
}

function Highlight({ text, query }: HighlightProps) {
  if (!query || query.length < 2 || !text) return <>{text}</>
  const regex = new RegExp(`(${query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'gi')
  const parts = text.split(regex)
  return (
    <>
      {parts.map((part, i) =>
        regex.test(part)
          ? <mark key={i} className="bg-[var(--brand-primary)]/20 text-[var(--brand-primary)] rounded-sm px-0.5">{part}</mark>
          : <span key={i}>{part}</span>
      )}
    </>
  )
}

// Rendered twice (desktop header and MobileHeader): each instance needs its own ids so its
// label and results list stay associated with its own input.
export default function GlobalSearch({ id = 'global-search' }: { id?: string } = {}) {
  const resultsId = `${id}-results`
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<SearchResults | null>(null)
  const [isOpen, setIsOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const navigate = useNavigate()
  const license = useLicense()

  // Close on click outside
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setIsOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  // Search shortcuts; slash must not interrupt typing in another editor.
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      const target = e.target instanceof HTMLElement ? e.target : null
      const editing = target?.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"])')
      const slash = e.key === '/' && !e.metaKey && !e.ctrlKey && !e.altKey && !editing
      if (((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') || slash) {
        e.preventDefault()
        inputRef.current?.focus()
        setIsOpen(true)
      }
      if (e.key === 'Escape') {
        setIsOpen(false)
        setQuery('')
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [])

  // Search on query change
  useEffect(() => {
    let active = true
    setResults(null)
    setLoading(false)
    if (query.length < 2) {
      return () => { active = false }
    }
    const timer = setTimeout(async () => {
      setLoading(true)
      try {
        const data = await search.query(query)
        if (!active) return
        setResults({ ...data, orders: license.isPro && canAccessPage('jobs') ? (data.orders || []) : [], products: license.isPro && canAccessPage('models') ? (data.products || []) : [] })
      } catch (err) {
        if (active) console.error('Search failed:', err)
      } finally {
        if (active) setLoading(false)
      }
    }, 300)
    return () => { active = false; clearTimeout(timer) }
  }, [query, license.isPro])

  const handleSelect = (type: string, id: number) => {
    setIsOpen(false)
    setQuery('')
    const routes: Record<string, string> = {
      order: '/orders',
      product: '/products',
      model: '/models',
      job: '/jobs',
      spool: '/spools',
      printer: '/printers',
    }
    const path = routes[type]
    if (path) {
      navigate(id ? `${path}?highlight=${id}` : path)
    }
  }

  const totalResults = results
    ? results.models.length + results.jobs.length + results.spools.length + results.printers.length + results.orders.length + results.products.length
    : 0

  return (
    <div ref={containerRef} className="relative">
      {/* Search Input */}
      <div className="relative" role="search">
        <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-[var(--brand-text-muted)]" aria-hidden="true" />
        <label htmlFor={id} className="sr-only">Search printers, jobs, models, spools, orders, products</label>
        <input
          id={id}
          ref={inputRef}
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onFocus={() => setIsOpen(true)}
          placeholder="Search... (⌘K)"
          role="combobox"
          aria-autocomplete="list"
          aria-expanded={isOpen && query.length >= 2}
          aria-controls={resultsId}
          aria-haspopup="listbox"
          className="w-44 md:w-64 bg-transparent border-b border-[var(--brand-card-border)] rounded-none pl-9 pr-8 py-1.5 text-sm placeholder-farm-500 focus:outline-hidden focus:border-[var(--brand-primary)]"
        />
        {query && (
          <button
            onClick={() => { setQuery(''); setResults(null) }}
            className="absolute right-2 top-1/2 -translate-y-1/2 text-[var(--brand-text-muted)] hover:text-[var(--brand-text-secondary)]"
            aria-label="Clear search"
          >
            <X size={14} />
          </button>
        )}
      </div>

      {/* Results Dropdown */}
      {isOpen && query.length >= 2 && (
        <div id={resultsId} role="listbox" aria-label="Search results" className="absolute top-full left-0 right-0 md:w-80 mt-2 bg-[var(--brand-card-bg)] border border-[var(--brand-card-border)] rounded-md shadow-xl z-50 max-h-96 overflow-auto">
          {loading ? (
            <div className="p-4 text-center text-[var(--brand-text-muted)] text-sm">Searching...</div>
          ) : totalResults === 0 ? (
            <div className="p-4 text-center text-[var(--brand-text-muted)] text-sm">No results for "{query}"</div>
          ) : (
            <>
              {(['orders', 'products'] as const).map(group => results![group].length > 0 && (
                <div key={group}>
                  <div className="px-3 py-2 text-xs font-medium text-[var(--brand-text-muted)] bg-[var(--brand-surface)]">{group === 'orders' ? 'Orders' : 'Products'}</div>
                  {results![group].map(item => <button key={`${group}-${item.id}`} aria-label={item.name} onClick={() => handleSelect(group === 'orders' ? 'order' : 'product', item.id)} className="w-full flex items-center gap-2 px-3 py-2 hover:bg-[var(--brand-surface)] text-left text-sm focus:bg-[var(--brand-surface)]"><Package size={14} /><span className="truncate"><Highlight text={item.name} query={query} /></span></button>)}
                </div>
              ))}
              {results!.models.length > 0 && (
                <div>
                  <div className="px-3 py-2 text-xs font-medium text-[var(--brand-text-muted)] bg-[var(--brand-surface)]">Models</div>
                  {results!.models.map((item) => (
                    <button
                      key={`model-${item.id}`}
                      onClick={() => handleSelect('model', item.id)}
                      className="w-full flex items-center gap-2 px-3 py-2 hover:bg-[var(--brand-surface)] text-left text-sm focus:bg-[var(--brand-surface)]"
                    >
                      <Package size={14} className="text-blue-400" />
                      <span className="truncate"><Highlight text={item.name} query={query} /></span>{item.qr_code && <span className="text-xs text-[var(--brand-text-muted)] ml-2"><Highlight text={item.qr_code} query={query} /></span>}
                    </button>
                  ))}
                </div>
              )}
              {results!.jobs.length > 0 && (
                <div>
                  <div className="px-3 py-2 text-xs font-medium text-[var(--brand-text-muted)] bg-[var(--brand-surface)]">Jobs</div>
                  {results!.jobs.map((item) => (
                    <button
                      key={`job-${item.id}`}
                      onClick={() => handleSelect('job', item.id)}
                      className="w-full flex items-center gap-2 px-3 py-2 hover:bg-[var(--brand-surface)] text-left text-sm focus:bg-[var(--brand-surface)]"
                    >
                      <ListTodo size={14} className="text-green-400" />
                      <span className="truncate flex-1"><Highlight text={item.name} query={query} /></span>
                      <span className={`text-xs px-1.5 py-0.5 rounded-lg ${
                        item.status === 'completed' ? 'bg-green-900/50 text-green-400' :
                        item.status === 'failed' ? 'bg-red-900/50 text-red-400' :
                        'bg-[var(--brand-surface)] text-[var(--brand-text-secondary)]'
                      }`}>{item.status}</span>
                    </button>
                  ))}
                </div>
              )}
              {results!.spools.length > 0 && (
                <div>
                  <div className="px-3 py-2 text-xs font-medium text-[var(--brand-text-muted)] bg-[var(--brand-surface)]">Spools</div>
                  {results!.spools.map((item) => (
                    <button
                      key={`spool-${item.id}`}
                      onClick={() => handleSelect('spool', item.id)}
                      className="w-full flex items-center gap-2 px-3 py-2 hover:bg-[var(--brand-surface)] text-left text-sm focus:bg-[var(--brand-surface)]"
                    >
                      <Database size={14} className="text-purple-400" />
                      <span className="truncate"><Highlight text={item.name} query={query} /></span>{item.qr_code && <span className="text-xs text-[var(--brand-text-muted)] ml-2"><Highlight text={item.qr_code} query={query} /></span>}
                    </button>
                  ))}
                </div>
              )}
              {results!.printers.length > 0 && (
                <div>
                  <div className="px-3 py-2 text-xs font-medium text-[var(--brand-text-muted)] bg-[var(--brand-surface)]">Printers</div>
                  {results!.printers.map((item) => (
                    <button
                      key={`printer-${item.id}`}
                      onClick={() => handleSelect('printer', item.id)}
                      className="w-full flex items-center gap-2 px-3 py-2 hover:bg-[var(--brand-surface)] text-left text-sm focus:bg-[var(--brand-surface)]"
                    >
                      <Printer size={14} className="text-orange-400" />
                      <span className="truncate"><Highlight text={item.name} query={query} /></span>{item.qr_code && <span className="text-xs text-[var(--brand-text-muted)] ml-2"><Highlight text={item.qr_code} query={query} /></span>}
                    </button>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}
