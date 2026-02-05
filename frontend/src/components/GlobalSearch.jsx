import { useState, useEffect, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { Search, X, Package, ListTodo, Printer, Database } from 'lucide-react'
import { search } from '../api'

export default function GlobalSearch() {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState(null)
  const [isOpen, setIsOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const inputRef = useRef(null)
  const containerRef = useRef(null)
  const navigate = useNavigate()

  // Close on click outside
  useEffect(() => {
    const handleClickOutside = (e) => {
      if (containerRef.current && !containerRef.current.contains(e.target)) {
        setIsOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  // Keyboard shortcut (Cmd+K or Ctrl+K)
  useEffect(() => {
    const handleKeyDown = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
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
    if (query.length < 2) {
      setResults(null)
      return
    }
    const timer = setTimeout(async () => {
      setLoading(true)
      try {
        const data = await search.query(query)
        setResults(data)
      } catch (err) {
        console.error('Search failed:', err)
      } finally {
        setLoading(false)
      }
    }, 300)
    return () => clearTimeout(timer)
  }, [query])

  const handleSelect = (type, id) => {
    setIsOpen(false)
    setQuery('')
    switch (type) {
      case 'model':
        navigate('/models')
        break
      case 'job':
        navigate('/jobs')
        break
      case 'spool':
        navigate('/spools')
        break
      case 'printer':
        navigate('/printers')
        break
    }
  }

  const totalResults = results 
    ? results.models.length + results.jobs.length + results.spools.length + results.printers.length 
    : 0

  return (
    <div ref={containerRef} className="relative">
      {/* Search Input */}
      <div className="relative">
        <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-farm-500" />
        <input
          ref={inputRef}
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onFocus={() => setIsOpen(true)}
          placeholder="Search... (⌘K)"
          className="w-full md:w-64 bg-farm-800 border border-farm-700 rounded-lg pl-9 pr-8 py-1.5 text-sm placeholder-farm-500 focus:outline-none focus:border-farm-600"
        />
        {query && (
          <button
            onClick={() => { setQuery(''); setResults(null) }}
            className="absolute right-2 top-1/2 -translate-y-1/2 text-farm-500 hover:text-farm-300"
          >
            <X size={14} />
          </button>
        )}
      </div>

      {/* Results Dropdown */}
      {isOpen && query.length >= 2 && (
        <div className="absolute top-full left-0 right-0 md:w-80 mt-2 bg-farm-900 border border-farm-700 rounded-lg shadow-xl z-50 max-h-96 overflow-auto">
          {loading ? (
            <div className="p-4 text-center text-farm-500 text-sm">Searching...</div>
          ) : totalResults === 0 ? (
            <div className="p-4 text-center text-farm-500 text-sm">No results for "{query}"</div>
          ) : (
            <>
              {results.models.length > 0 && (
                <div>
                  <div className="px-3 py-2 text-xs font-medium text-farm-500 bg-farm-800/50">Models</div>
                  {results.models.map((item) => (
                    <button
                      key={`model-${item.id}`}
                      onClick={() => handleSelect('model', item.id)}
                      className="w-full flex items-center gap-2 px-3 py-2 hover:bg-farm-800 text-left text-sm"
                    >
                      <Package size={14} className="text-blue-400" />
                      <span className="truncate">{item.name}</span>
                    </button>
                  ))}
                </div>
              )}
              {results.jobs.length > 0 && (
                <div>
                  <div className="px-3 py-2 text-xs font-medium text-farm-500 bg-farm-800/50">Jobs</div>
                  {results.jobs.map((item) => (
                    <button
                      key={`job-${item.id}`}
                      onClick={() => handleSelect('job', item.id)}
                      className="w-full flex items-center gap-2 px-3 py-2 hover:bg-farm-800 text-left text-sm"
                    >
                      <ListTodo size={14} className="text-green-400" />
                      <span className="truncate flex-1">{item.name}</span>
                      <span className={`text-xs px-1.5 py-0.5 rounded ${
                        item.status === 'completed' ? 'bg-green-900/50 text-green-400' :
                        item.status === 'failed' ? 'bg-red-900/50 text-red-400' :
                        'bg-farm-700 text-farm-400'
                      }`}>{item.status}</span>
                    </button>
                  ))}
                </div>
              )}
              {results.spools.length > 0 && (
                <div>
                  <div className="px-3 py-2 text-xs font-medium text-farm-500 bg-farm-800/50">Spools</div>
                  {results.spools.map((item) => (
                    <button
                      key={`spool-${item.id}`}
                      onClick={() => handleSelect('spool', item.id)}
                      className="w-full flex items-center gap-2 px-3 py-2 hover:bg-farm-800 text-left text-sm"
                    >
                      <Database size={14} className="text-purple-400" />
                      <span className="truncate">{item.name}</span>
                    </button>
                  ))}
                </div>
              )}
              {results.printers.length > 0 && (
                <div>
                  <div className="px-3 py-2 text-xs font-medium text-farm-500 bg-farm-800/50">Printers</div>
                  {results.printers.map((item) => (
                    <button
                      key={`printer-${item.id}`}
                      onClick={() => handleSelect('printer', item.id)}
                      className="w-full flex items-center gap-2 px-3 py-2 hover:bg-farm-800 text-left text-sm"
                    >
                      <Printer size={14} className="text-orange-400" />
                      <span className="truncate">{item.name}</span>
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
