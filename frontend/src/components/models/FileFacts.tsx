import { useState } from 'react'
import { ChevronDown } from 'lucide-react'
import clsx from 'clsx'
import type { FileFacts, PrinterFit } from '../../api/models'
import { getColorName } from '../../utils/colorNames'

// "Runs on N of your printers", counted by the server with the scheduler's own checks.
// The names open on click; with no fit it says which printer the file was sliced for.
export function PrinterFitLine({ fit, className = '' }: { fit?: PrinterFit | null; className?: string }) {
  const [open, setOpen] = useState(false)
  if (!fit || typeof fit.count !== 'number') return null
  if (fit.count === 0) {
    return (
      <p className={clsx('text-xs text-[var(--brand-muted)] break-words', className)}>
        Runs on none of your printers.{fit.sliced_for ? ` It was sliced for ${fit.sliced_for}.` : ''}
      </p>
    )
  }
  return (
    <div className={clsx('text-xs min-w-0', className)}>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen(value => !value)}
        className="inline-flex max-w-full items-center gap-1 text-left text-[var(--brand-text-secondary)] hover:underline"
      >
        <span>Runs on {fit.count} of your printers</span>
        <ChevronDown size={12} aria-hidden="true" className={clsx('shrink-0 transition-transform', open && 'rotate-180')} />
      </button>
      {open && (
        <ul aria-label="Printers this file runs on" className="mt-1 flex flex-wrap gap-1">
          {fit.printers.map(printer => (
            <li key={printer.id} className="px-2 py-0.5 rounded-md bg-[var(--brand-input-bg)] text-[var(--brand-text-secondary)] break-all">{printer.name}</li>
          ))}
        </ul>
      )}
    </div>
  )
}

const plateLabel = (value: string) => {
  const text = value.replace(/_/g, ' ').trim()
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : ''
}

// What the file says about how it was sliced. A fact the file does not state is not listed.
export function fileFactLabels(facts?: FileFacts | null): string[] {
  if (!facts) return []
  const labels: string[] = []
  if (typeof facts.nozzle_diameter === 'number' && facts.nozzle_diameter > 0) labels.push(`${facts.nozzle_diameter} mm nozzle`)
  if (typeof facts.layer_height === 'number' && facts.layer_height > 0) labels.push(`${facts.layer_height} mm layers`)
  if (typeof facts.plate_type === 'string' && plateLabel(facts.plate_type)) labels.push(plateLabel(facts.plate_type))
  if (typeof facts.supports === 'boolean') labels.push(facts.supports ? 'Supports on' : 'Supports off')
  const colors = new Set<string>()
  for (const hex of Array.isArray(facts.filament_colors) ? facts.filament_colors : []) {
    const name = /^#?[0-9a-fA-F]{6}$/.test(String(hex)) ? getColorName(String(hex)) : null
    if (name) colors.add(name)
  }
  return [...labels, ...colors]
}

export function FileFactChips({ facts, className = '' }: { facts?: FileFacts | null; className?: string }) {
  const labels = fileFactLabels(facts)
  if (!labels.length) return null
  return (
    <ul aria-label="File details" className={clsx('flex flex-wrap gap-1', className)}>
      {labels.map(label => (
        <li key={label} className="px-2 py-0.5 bg-[var(--brand-card-border)]/60 text-[var(--brand-text-secondary)] rounded-md text-xs">{label}</li>
      ))}
    </ul>
  )
}
