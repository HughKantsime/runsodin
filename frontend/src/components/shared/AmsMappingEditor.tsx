import { useState } from 'react'
import type { ArchiveAmsPreview, AmsMapping } from '../../types/archive'

export type { AmsMapping } from '../../types/archive'

// The context key prevents a selection for one file/printer leaking into another.
export function useAmsMapping(preview: ArchiveAmsPreview | null | undefined, context: string, stored?: number[] | null) {
  const [choice, setChoice] = useState<{ context: string; mapping: AmsMapping; enabled: boolean } | null>(null)
  const initial = stored?.map((target, source) => ({ source_slot: source, target_slot: target + 1 }))
    ?? preview?.suggested_mapping.map(({ source_slot, target_slot }) => ({ source_slot, target_slot })) ?? []
  const complete = (mapping: AmsMapping) => !!preview?.required.length
    && preview.required.every(req => mapping.some(m => m.source_slot === req.slot && preview.available.some(s => s.slot === m.target_slot)))
    && new Set(mapping.map(m => m.target_slot)).size === mapping.length
  const selection = choice?.context === context ? choice : { context, mapping: initial, enabled: complete(initial) }
  const valid = complete(selection.mapping)
  return {
    mapping: selection.mapping, enabled: selection.enabled, valid,
    value: selection.enabled && valid ? selection.mapping : undefined,
    onChange: (mapping: AmsMapping) => setChoice({ ...selection, mapping }),
    onEnabledChange: (enabled: boolean) => setChoice({ ...selection, enabled }),
  }
}

export default function AmsMappingEditor({ preview, loading, error, mapping, enabled, valid, onChange, onEnabledChange }: {
  preview?: ArchiveAmsPreview | null; loading?: boolean; error?: boolean;
  mapping: AmsMapping; enabled: boolean; valid: boolean;
  onChange: (value: AmsMapping) => void; onEnabledChange: (value: boolean) => void;
}) {
  return <section aria-label="AMS filament mapping" className="space-y-2 my-3">
    <h3 className="text-sm font-medium">AMS filament mapping</h3>
    {loading ? <p role="status">Loading filament mapping...</p>
      : error ? <p role="alert">Filament mapping preview unavailable</p>
      : !preview?.required.length ? <p className="text-xs">The original file lists no filaments.</p>
      : <>
        <ul className="space-y-2">{preview.required.map(req => {
          const match = mapping.find(m => m.source_slot === req.slot)
          return <li key={req.slot} className="flex items-center gap-2 text-xs">
            <span className="inline-block w-3 h-3 rounded-full border border-[var(--brand-card-border)]" style={{ background: req.color || 'transparent' }} />
            <label htmlFor={`ams-source-${req.slot}`}>Filament {req.slot + 1}: {req.material || 'Unknown'} {req.color}</label>
            <span aria-hidden="true">→</span>
            <select id={`ams-source-${req.slot}`} aria-label={`Filament ${req.slot + 1} AMS slot`}
              className="bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded p-1"
              value={match?.target_slot ?? ''} onChange={e => onChange([
                ...mapping.filter(m => m.source_slot !== req.slot),
                ...(e.target.value ? [{ source_slot: req.slot, target_slot: Number(e.target.value) }] : []),
              ].sort((a, b) => a.source_slot - b.source_slot))}>
              <option value="">No matching slot</option>
              {preview.available.map(slot => <option key={slot.slot} value={slot.slot}>Slot {slot.slot} ({[slot.color, slot.material].filter(Boolean).join(' ')})</option>)}
            </select>
          </li>
        })}</ul>
        <label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={enabled} onChange={e => onEnabledChange(e.target.checked)} />Use this mapping</label>
        {enabled && !valid && <p role="alert">Choose a distinct loaded slot for every filament.</p>}
        <p className="text-xs text-[var(--brand-text-muted)]">Confirm the loaded materials before saving. Suggestions are not applied until you confirm.</p>
      </>}
  </section>
}
