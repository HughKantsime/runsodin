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
  if (slot.spool_change_pending) return 'changed'
  if (slot.filament_type === 'empty') return 'empty'
  if (['SUPPORT', 'PLA_SUPPORT', 'PVA', 'HIPS'].includes(slot.filament_type || '')) return 'support'
  return 'loaded'
}
/** Feeding/blocked are explicit states only: current HTTP slot data exposes neither flag. */
export default function SlotTile({ slot, apiType, state: explicitState, label, showState = true }: { slot: SlotData; apiType?: string | null; state?: SlotState; label?: string; showState?: boolean }) {
  const state = explicitState || slotState(slot)
  const unavailable = slot.mapping_status && slot.mapping_status !== 'mapped'
  const named = slot.color && !/^preset:|^#?[a-f0-9]{6}$/i.test(slot.color) ? slot.color : null
  const material = unavailable ? (slot.mapping_status === 'unavailable' ? 'Spool unavailable' : 'Unmapped') : state === 'empty' ? 'Empty' : named || slot.material_type || (slot.filament_type !== 'empty' && slot.filament_type) || 'Material unknown'
  const color = slot.color_hex?.replace(/^#/, '')
  const remaining = !unavailable && number(slot.remaining_weight) && slot.remaining_weight >= 0 ? `${Math.round(slot.remaining_weight)} g` : !unavailable && number(slot.remaining) && slot.remaining >= 0 && slot.remaining <= 100 ? `${Math.round(slot.remaining)}%` : 'unknown'
  return <span className="f-slot" data-slot-state={state}>
    <span className="f-swatch" aria-hidden="true" style={{ backgroundColor: !unavailable && state !== 'empty' && color && /^[a-f0-9]{6,8}$/i.test(color) ? `#${color}` : undefined }} />
    <span className="f-slot-label" title={material}>{material}</span>
    {slot.preset && !unavailable && <span className="f-slot-preset" title={slot.preset}>Preset: {slot.preset.replace(/^preset:\s*/i, '')}</span>}
    <span className="f-mono">{label || slot.display_name || slotLabel(apiType, slot.slot_number)}</span>
    {!unavailable && showState && <span className="f-slot-state">{explicitState || slot.filament_type || slot.material_type ? labels[state] : 'State unknown'}</span>}
    <span className="f-mono">Remaining {remaining}</span>
  </span>
}
