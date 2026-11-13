import { useFoundry } from '../../hooks/themePreferences'
import SlotTile, { slotLabel as deviceSlotLabel } from '../foundry/SlotTile'
import { useState } from 'react'
import type { ArchiveAmsPreview, ArchiveAmsSlot, AmsMapping } from '../../types/archive'

export type { AmsMapping } from '../../types/archive'

// The context key prevents a selection for one file/printer leaking into another.
export function useAmsMapping(preview: ArchiveAmsPreview | null | undefined, context: string, stored?: number[] | null) {
  const [choice, setChoice] = useState<{ context: string; mapping: AmsMapping; enabled: boolean } | null>(null)
  const initial = stored?.flatMap((target, source) => target < 0 ? [] : [{ source_slot: source + 1, target_slot: target + 1 }])
    ?? preview?.suggested_mapping.map(({ source_slot, target_slot }) => ({ source_slot, target_slot })) ?? []
  const complete = (mapping: AmsMapping) => !!preview?.required.length
    && preview.required.every(req => mapping.some(m => m.source_slot === req.slot && preview.available.some(s => s.slot === m.target_slot && !s.do_not_feed)))
    && new Set(mapping.map(m => m.target_slot)).size === mapping.length
  const needsConfirmation = !stored && !!preview?.confirm_suggestions && preview.suggested_mapping.some(s => s.confidence === 'material_only')
  const selection = choice?.context === context ? choice : { context, mapping: initial, enabled: complete(initial) && !needsConfirmation }
  const valid = complete(selection.mapping)
  return {
    mapping: selection.mapping, enabled: selection.enabled, valid,
    value: selection.enabled && valid ? selection.mapping : undefined,
    // Picking the slots is the confirmation a held-back suggestion was waiting for.
    onChange: (mapping: AmsMapping) => setChoice({ ...selection, mapping, enabled: selection.enabled || (!!preview?.confirm_suggestions && complete(mapping)) }),
    onEnabledChange: (enabled: boolean) => setChoice({ ...selection, enabled }),
  }
}

// An S1 slot's colour name is only its material, so its hex is what tells same-material slots apart.
function slotLabel(slot: ArchiveAmsSlot) {
  const name = slot.color && slot.color !== slot.material ? slot.color : null
  const hex = !name && slot.color_hex ? `#${slot.color_hex.replace(/^#/, '')}` : null
  return [name, slot.material, hex].filter(Boolean).join(' ')
}

export default function AmsMappingEditor({ preview, loading, error, mapping, enabled, valid, onChange, onEnabledChange, planned = false }: {
  planned?: boolean; preview?: ArchiveAmsPreview | null; loading?: boolean; error?: boolean;
  mapping: AmsMapping; enabled: boolean; valid: boolean;
  onChange: (value: AmsMapping) => void; onEnabledChange: (value: boolean) => void;
}) {
  const foundry = useFoundry()
  return <section aria-label="AMS filament mapping" className="space-y-2 my-3">
    <h3 className="text-sm font-medium">AMS filament mapping</h3>
    {loading ? <p role="status">Loading filament mapping...</p>
      : error ? <p role="alert">Filament mapping preview unavailable</p>
      : !preview?.required.length ? <p className="text-xs">{preview?.file_note || 'The original file lists no filaments.'}</p>
      : <>
        <ul className="space-y-2">{preview.required.map(req => {
          const match = mapping.find(m => m.source_slot === req.slot)
          return <li key={req.slot} className="flex items-center gap-2 text-xs">
            <span className="inline-block w-3 h-3 rounded-full border border-[var(--brand-card-border)]" style={{ background: req.color || 'transparent' }} />
            <label htmlFor={`ams-source-${req.slot}`}>Filament {req.slot}: {req.material || 'Unknown'} {req.color}</label>
            <span aria-hidden="true">→</span>
            {foundry ? <div className="f-slots" role="group" aria-label={`Filament ${req.slot} AMS slot`}>
              <button type="button" aria-pressed={!match} onClick={() => onChange(mapping.filter(m => m.source_slot !== req.slot))}>No matching slot</button>
              {preview.available.map(slot => <button type="button" className="f-slot-button" key={slot.slot} disabled={slot.do_not_feed} aria-label={`${deviceSlotLabel(preview.confirm_suggestions ? "anycubic_lan" : "bambu", slot.slot)} (${slotLabel(slot)})${slot.do_not_feed ? " — Do not feed" : ""}`} aria-pressed={match?.target_slot === slot.slot}
                onClick={() => onChange([...mapping.filter(m => m.source_slot !== req.slot), { source_slot: req.slot, target_slot: slot.slot }].sort((a, b) => a.source_slot - b.source_slot))}>
                <SlotTile apiType={preview.confirm_suggestions ? "anycubic_lan" : "bambu"} slot={{ slot_number: slot.slot, material_type: slot.material, color: slotLabel(slot), color_hex: slot.color_hex, do_not_feed: slot.do_not_feed }} />
              </button>)}
            </div> : (            <select id={`ams-source-${req.slot}`} aria-label={`Filament ${req.slot} AMS slot`}
              className="bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded p-1"
              value={match?.target_slot ?? ''} onChange={e => onChange([
                ...mapping.filter(m => m.source_slot !== req.slot),
                ...(e.target.value ? [{ source_slot: req.slot, target_slot: Number(e.target.value) }] : []),
              ].sort((a, b) => a.source_slot - b.source_slot))}>
              <option value="">No matching slot</option>
              {preview.available.map(slot => <option key={slot.slot} value={slot.slot} disabled={slot.do_not_feed}>Slot {slot.slot} ({slotLabel(slot)}){slot.do_not_feed ? ' — Do not feed' : ''}</option>)}
            </select>)}
          </li>
        })}</ul>
        <label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={enabled} onChange={e => onEnabledChange(e.target.checked)} />Use this mapping</label>
        {enabled && !valid && <p role="alert">Choose a distinct slot for every filament.</p>}
        <p className="text-xs text-[var(--brand-text-muted)]">{planned ? 'Plan the trays now; load the matching materials before releasing the hold.' : 'Confirm the loaded materials before saving. Suggestions are not applied until you confirm.'}</p>
      </>}
  </section>
}
