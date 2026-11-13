import type { FilamentSlot } from '../../types'
import { number } from './data'

export type SlotState = 'loaded' | 'feeding' | 'empty' | 'blocked' | 'changed' | 'support'
export type SlotData = Omit<Partial<FilamentSlot>, 'filament_type'> & { slot_number: number; filament_type?: string; display_name?: string | null; mapping_status?: string | null }
const labels: Record<SlotState, string> = { loaded: 'Loaded', feeding: 'Feeding now', empty: 'No spool', blocked: 'Do not feed', changed: 'Spool changed?', support: 'Support only' }
export function slotLabel(apiType: string | null | undefined, n: number) {
  if (apiType === 'bambu' && n >= 17 && n <= 24) return n === 17 ? 'HT' : `HT ${n - 16}`
  if (apiType === 'bambu' && n >= 1 && n <= 16) return `AMS ${Math.floor((n - 1) / 4) + 1} · ${(n - 1) % 4 + 1}`
  if (apiType === 'anycubic_lan') return n <= 4 ? `ACE · ${n}` : `ACE ${Math.floor((n - 1) / 4) + 1} · ${(n - 1) % 4 + 1}`
  return `Slot ${n}`
}
export function slotState(slot: SlotData): SlotState {
  if (slot.do_not_feed) return 'blocked'
  if (slot.spool_change_pending) return 'changed'
  if (slot.filament_type === 'empty') return 'empty'
  if (['SUPPORT', 'PLA_SUPPORT', 'PVA', 'HIPS'].includes(slot.filament_type || '')) return 'support'
  return 'loaded'
}
/** Feeding/blocked are explicit observations, never inferred from material. */
export default function SlotTile({ slot, apiType, state: explicitState, label, showState = true, variant = 'full' }: { slot: SlotData; apiType?: string | null; state?: SlotState; label?: string; showState?: boolean; variant?: 'full' | 'compact' }) {
  const state = slot.do_not_feed ? 'blocked' : explicitState || slotState(slot)
  const unavailable = slot.mapping_status && slot.mapping_status !== 'mapped'
  const named = slot.color && !/^preset:|^#?[a-f0-9]{6}$/i.test(slot.color) ? slot.color : null
  const material = unavailable ? (slot.mapping_status === 'unavailable' ? 'Spool unavailable' : 'Unmapped') : state === 'empty' ? 'Empty' : (variant === 'compact' ? slot.material_type || (slot.filament_type !== 'empty' && slot.filament_type) : named) || slot.material_type || (slot.filament_type !== 'empty' && slot.filament_type) || 'Material unknown'
  const color = slot.color_hex?.replace(/^#/, '')
  const remaining = !unavailable && number(slot.remaining_weight) && slot.remaining_weight >= 0 ? `${Math.round(slot.remaining_weight)} g` : !unavailable && number(slot.remaining) && slot.remaining >= 0 && slot.remaining <= 100 ? `${Math.round(slot.remaining)}%` : !unavailable && state !== 'empty' && (slot as any).assigned_spool_id != null ? 'not measured on this printer model' : 'unknown'
  return <span className="f-slot" data-slot-state={state} data-variant={variant} title={variant === 'compact' ? `${material} · ${label || slotLabel(apiType, slot.slot_number)} · ${labels[state]}` : undefined}>
    <span className="f-swatch" aria-hidden="true" style={{ backgroundColor: !unavailable && state !== 'empty' && color && /^[a-f0-9]{6,8}$/i.test(color) ? `#${color}` : undefined }} />
    <span className="f-slot-label" title={material}>{material}</span>
    {variant === 'full' && slot.preset && !unavailable && <span className="f-slot-preset" title={slot.preset}>Preset: {slot.preset.replace(/^preset:\s*/i, '')}</span>}
    <span className="f-mono">{label || (variant === 'compact' ? (apiType === 'bambu' && slot.slot_number >= 17 ? slotLabel(apiType, slot.slot_number) : slot.slot_number) : slot.display_name || slotLabel(apiType, slot.slot_number))}</span>
    {!unavailable && showState && <span title={slot.do_not_feed ? slot.do_not_feed_note || 'Do not feed' : undefined} className={variant === 'compact' ? 'sr-only' : 'f-slot-state'}>{slot.do_not_feed || explicitState || slot.filament_type || slot.material_type ? labels[state] : 'State unknown'}</span>}
    {variant === 'full' && <span className="f-mono">Remaining {remaining}</span>}
  </span>
}
