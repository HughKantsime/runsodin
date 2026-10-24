import ModelPlates from './ModelPlates'
import { useState, useEffect, useRef } from 'react'
import { Pencil, Trash2, Clock, Scale, Star, Package, CalendarPlus, History, X, ChevronDown, Palette, DollarSign, EyeOff } from 'lucide-react'
import clsx from 'clsx'
import { canDo } from '../../permissions'
import { fetchAPI } from '../../api/client'
import { useQueryClient } from '@tanstack/react-query'
import toast from 'react-hot-toast'

export function ModelCard({  model, onEdit, onDelete, onSchedule, onToggleFavorite, onView3D, onRevisions, submitForApproval = false }) {
  const queryClient = useQueryClient()
  return (
    <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] overflow-hidden hover:border-[var(--brand-card-border)] transition-colors">
      <div className="h-28 md:h-32 bg-[var(--brand-content-bg)] flex items-center justify-center">
        {model.thumbnail_b64 ? (
          <img
            src={`data:image/png;base64,${model.thumbnail_b64}`}
            alt={model.name}
            className="h-full w-full object-contain p-1"
          />
        ) : model.thumbnail_url ? (
          <img
            src={model.thumbnail_url}
            alt={model.name}
            className="h-full w-full object-cover"
          />
        ) : (
          <Package size={36} className="text-[var(--brand-muted)]" />
        )}
      </div>
      <div className="p-3 md:p-4">
        <div className="flex flex-col mb-2 gap-2">
          <div className="min-w-0 w-full">
            <h3 className="font-medium text-sm md:text-base truncate">{model.name}</h3>
            <div className="flex items-center gap-2 mt-0.5">
              {model.default_filament_type && (
                <span className="text-xs px-1.5 py-0.5 bg-[var(--brand-input-bg)] text-[var(--brand-muted)] rounded-md">{model.default_filament_type}</span>
              )}
              {model.variant_count > 1 && (
                <span className="text-xs px-1.5 py-0.5 bg-blue-500/20 text-blue-300 rounded-md">{model.variant_count} variants</span>
              )}
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-0.5">
            <button
              onClick={() => onView3D(model)}
              className="p-1.5 md:p-2 text-[var(--brand-muted)] hover:bg-[var(--brand-input-bg)] rounded-md transition-colors"
              aria-label="View 3D preview"
            >
              <Package size={14} />
            </button>
            <button
              onClick={() => onToggleFavorite(model)}
              className={clsx("p-1.5 md:p-2 rounded-md transition-colors", model.is_favorite ? "text-yellow-400" : "text-[var(--brand-muted)] hover:text-yellow-400")}
              aria-label={model.is_favorite ? "Remove from favorites" : "Add to favorites"}
            >
              <Star size={14} fill={model.is_favorite ? "currentColor" : "none"} />
            </button>

            {(canDo('models.create') || submitForApproval) && <button
              onClick={() => onSchedule(model)}
              className="p-1.5 md:p-2 text-[var(--brand-primary)] hover:bg-[var(--brand-primary)]/10 rounded-md transition-colors"
              aria-label={submitForApproval ? 'Submit for approval' : 'Schedule print job'}
            >
              <CalendarPlus size={14} />
            </button>}
            {canDo('models.edit') && <button
              onClick={() => onEdit(model)}
              className="p-1.5 md:p-2 text-[var(--brand-muted)] hover:bg-[var(--brand-input-bg)] rounded-md transition-colors"
              aria-label="Edit model"
            >
              <Pencil size={14} />
            </button>}
            <button
              onClick={() => onRevisions(model)}
              className="p-1.5 md:p-2 text-[var(--brand-muted)] hover:bg-[var(--brand-input-bg)] rounded-md transition-colors"
              aria-label="View revisions"
            >
              <History size={14} />
            </button>
            {canDo('models.edit') && <button aria-label="Hide from library" onClick={async () => {
              try {
                const result = await fetchAPI<{ hidden: boolean }>(`/models/${model.id}/hide`, { method: 'POST' })
                toast.success(result.hidden ? 'Hidden from library' : 'Model remains visible while Education work is live')
                await queryClient.invalidateQueries({ queryKey: ['models'] })
              }
              catch (error) { toast.error(error instanceof Error ? error.message : 'Hide failed') }
            }}><EyeOff size={14} /></button>}
            {canDo('models.delete') && <button
              onClick={() => onDelete(model.id)}
              className="p-1.5 md:p-2 text-[var(--brand-muted)] hover:text-red-400 hover:bg-red-900/50 rounded-md transition-colors"
              aria-label="Delete model"
            >
              <Trash2 size={14} />
            </button>}
          </div>
        </div>

        <div className="flex items-center gap-3 md:gap-4 text-xs md:text-sm text-[var(--brand-muted)] mt-3">
          {model.build_time_hours > 0 && (
            <div className="flex items-center gap-1">
              <Clock size={12} />
              <span>{model.build_time_hours < 1 ? `${Math.round(model.build_time_hours * 60)}m` : `${model.build_time_hours.toFixed(1)}h`}</span>
            </div>
          )}
          {model.total_filament_grams > 0 && (
            <div className="flex items-center gap-1">
              <Scale size={12} />
              <span>{model.total_filament_grams.toFixed(0)}g</span>
            </div>
          )}
        </div>

        {canDo('models.plates') && <ModelPlates model={model} onSchedule={onSchedule} />}

        {(model.estimated_cost > 0 || model.suggested_price > 0) && (
          <div className="flex items-center justify-between mt-3 pt-3 border-t border-[var(--brand-card-border)]">
            <div className="text-xs text-[var(--brand-muted)]">
              Cost: <span className="text-[var(--brand-text-secondary)]">${model.estimated_cost?.toFixed(2) || '—'}</span>
            </div>
            <div className="text-xs font-medium text-green-400">
              ${model.suggested_price?.toFixed(2) || '—'}
            </div>
          </div>
        )}

        {model.required_colors?.length > 0 && (
          <div className="flex flex-wrap gap-1 mt-3">
            {model.required_colors.map((color, i) => (
              <div
                key={i}
                className="w-5 h-5 rounded-full border border-[var(--brand-card-border)]"
                style={{ backgroundColor: color }}
                title={color}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

export function FilamentSelector({ value, onChange, filamentData, placeholder }) {
  const [isOpen, setIsOpen] = useState(false)
  const [search, setSearch] = useState('')
  const [displayValue, setDisplayValue] = useState(value || '')
  const containerRef = useRef(null)

  useEffect(() => { setDisplayValue(value || '') }, [value])

  useEffect(() => {
    const handleClickOutside = (e) => {
      if (containerRef.current && !containerRef.current.contains(e.target)) {
        setIsOpen(false)
        setSearch('')
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  const filterText = isOpen ? search : ''
  const filteredFilaments = (filamentData || []).filter(f => {
    if (!filterText) return true
    return f.display_name?.toLowerCase().includes(filterText.toLowerCase()) ||
      f.name?.toLowerCase().includes(filterText.toLowerCase()) ||
      f.brand?.toLowerCase().includes(filterText.toLowerCase())
  })

  const spoolmanFilaments = filteredFilaments.filter(f => f.source === 'spoolman')
  const libraryFilaments = filteredFilaments.filter(f => f.source === 'library')

  const handleSelect = (filament) => {
    const colorName = filament.display_name || `${filament.brand} ${filament.name} (${filament.material})`
    onChange(colorName)
    setDisplayValue(colorName)
    setSearch('')
    setIsOpen(false)
  }

  const handleInputChange = (e) => {
    const val = e.target.value
    setSearch(val)
    setDisplayValue(val)
    onChange(val)
  }

  const handleFocus = () => { setIsOpen(true); setSearch('') }
  const handleBlur = () => { setTimeout(() => { setIsOpen(false); setSearch('') }, 150) }

  return (
    <div className="relative" ref={containerRef}>
      <div className="relative">
        <input
          type="text"
          value={isOpen ? search : displayValue}
          onChange={handleInputChange}
          onFocus={handleFocus}
          onBlur={handleBlur}
          className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-sm pr-8"
          placeholder={isOpen ? "Type to search..." : placeholder}
          aria-label={placeholder}
        />
        <button aria-label={isOpen ? "Close filament list" : "Open filament list"}
          type="button"
          onMouseDown={(e) => {
            e.preventDefault()
            if (isOpen) { setIsOpen(false); setSearch('') } else { setIsOpen(true); setSearch('') }
          }}
          className="absolute right-2 top-1/2 -translate-y-1/2 text-[var(--brand-muted)] hover:text-[var(--brand-text-secondary)]"
        >
          <ChevronDown size={16} className={clsx(isOpen && "rotate-180 transition-transform")} />
        </button>
      </div>

      {isOpen && (spoolmanFilaments.length > 0 || libraryFilaments.length > 0) && (
        <div className="absolute z-50 mt-1 w-full max-h-56 overflow-auto bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md shadow-lg">
          {spoolmanFilaments.length > 0 && (
            <>
              <div className="text-xs text-[var(--brand-primary)] font-medium px-3 py-1 bg-[var(--brand-card-bg)] sticky top-0">From Spoolman</div>
              {spoolmanFilaments.slice(0, 15).map(f => (
                <div key={f.id} onMouseDown={(e) => { e.preventDefault(); handleSelect(f) }} className="flex items-center gap-2 px-3 py-1.5 hover:bg-[var(--brand-card-border)] cursor-pointer">
                  <div className="w-4 h-4 rounded-md border border-[var(--brand-card-border)] shrink-0" style={{ backgroundColor: f.color_hex ? `#${f.color_hex}` : '#666' }} />
                  <span className="text-sm truncate flex-1">{f.name}</span>
                  {f.remaining_weight && <span className="text-xs text-[var(--brand-muted)]">{Math.round(f.remaining_weight)}g</span>}
                </div>
              ))}
            </>
          )}
          {libraryFilaments.length > 0 && (
            <>
              <div className="text-xs text-[var(--brand-muted)] font-medium px-3 py-1 bg-[var(--brand-card-bg)] sticky top-0">From Library</div>
              {libraryFilaments.slice(0, 20).map(f => (
                <div key={f.id} onMouseDown={(e) => { e.preventDefault(); handleSelect(f) }} className="flex items-center gap-2 px-3 py-1.5 hover:bg-[var(--brand-card-border)] cursor-pointer">
                  <div className="w-4 h-4 rounded-md border border-[var(--brand-card-border)] shrink-0" style={{ backgroundColor: f.color_hex ? `#${f.color_hex}` : '#666' }} />
                  <span className="text-sm truncate">{f.display_name}</span>
                </div>
              ))}
            </>
          )}
        </div>
      )}
    </div>
  )
}

export function ColorSlotInput({ index, color, grams, onChange, onRemove, filamentData }) {
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1">
        <FilamentSelector value={color} onChange={(val) => onChange(index, 'color', val)} filamentData={filamentData} placeholder={`Color ${index + 1}`} />
      </div>
      <div className="w-20 md:w-24">
        <input aria-label={`Color ${index + 1} grams`} type="number" min="0" step="0.1" value={grams} onChange={(e) => onChange(index, 'grams', e.target.value)} className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-2 md:px-3 py-2 text-sm" placeholder="g" />
      </div>
      <button aria-label={`Remove color ${index + 1}`} type="button" onClick={() => onRemove(index)} className="p-2 text-[var(--brand-muted)] hover:text-red-400 hover:bg-red-900/30 rounded-md transition-colors">
        <X size={16} />
      </button>
    </div>
  )
}

