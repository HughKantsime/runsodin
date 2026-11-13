import { useEffect, useState } from 'react'

export interface TrainingCrop { x: number; y: number; w: number; h: number }
type Key = keyof TrainingCrop
type Fields = Record<Key, string>

const FIELDS: [Key, string][] = [['x', 'Left'], ['y', 'Top'], ['w', 'Width'], ['h', 'Height']]
const MIN_SIZE = 0.05
const FULL: Fields = { x: '0', y: '0', w: '1', h: '1' }

const toFields = (crop?: TrainingCrop | null): Fields =>
  crop ? { x: String(crop.x), y: String(crop.y), w: String(crop.w), h: String(crop.h) } : FULL

/** Same rules as the server: fractions of the frame, inside 0..1, at least 5% wide and tall. */
export function parseCrop(fields: Fields): { crop?: TrainingCrop; error?: string } {
  const values = {} as TrainingCrop
  for (const [key, label] of FIELDS) {
    const raw = fields[key].trim()
    const n = Number(raw)
    if (raw === '' || !Number.isFinite(n)) return { error: `${label} must be a number from 0 to 1` }
    values[key] = Math.round(n * 10000) / 10000
  }
  if (values.x < 0 || values.x >= 1 || values.y < 0 || values.y >= 1) {
    return { error: 'Left and top must be at least 0 and below 1' }
  }
  if (values.w < MIN_SIZE || values.h < MIN_SIZE || values.w > 1 || values.h > 1) {
    return { error: `Width and height must be from ${MIN_SIZE} to 1` }
  }
  if (values.x + values.w > 1 + 1e-9 || values.y + values.h > 1 + 1e-9) {
    return { error: 'The crop must stay inside the frame' }
  }
  return { crop: values }
}

interface Props {
  name: string
  crop?: TrainingCrop | null
  onSave: (crop: TrainingCrop | null) => Promise<void>
}

export default function VisionTrainingCrop({ name, crop, onSave }: Props) {
  const [fields, setFields] = useState<Fields>(toFields(crop))
  const [status, setStatus] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => { setFields(toFields(crop)) }, [crop?.x, crop?.y, crop?.w, crop?.h])

  const run = async (value: TrainingCrop | null) => {
    setBusy(true)
    try {
      await onSave(value)
      setFields(toFields(value))
      setStatus(value ? 'Crop saved' : 'Full frame')
    } catch {
      setStatus('Save failed')
    }
    setBusy(false)
  }

  const save = () => {
    const parsed = parseCrop(fields)
    if (parsed.error) { setStatus(parsed.error); return }
    void run(parsed.crop as TrainingCrop)
  }

  return (
    <div className="space-y-1.5 text-xs">
      <div className="text-[var(--brand-text-muted)]">
        Training bed crop (fractions of the frame, applied to saved training frames only; detection frames stay full):
        {' '}{crop ? 'cropped' : 'full frame'}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {FIELDS.map(([key, label]) => (
          <label key={key} className="flex items-center gap-1">
            <span className="text-[var(--brand-text-muted)]">{label}</span>
            <input
              aria-label={`Training crop ${label} for ${name}`}
              type="number" min="0" max="1" step="0.01"
              value={fields[key]}
              onChange={e => setFields(f => ({ ...f, [key]: e.target.value }))}
              className="w-16 bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-1 py-0.5"
            />
          </label>
        ))}
        <button type="button" onClick={save} disabled={busy}
          className="px-2 py-1 bg-[var(--brand-primary)] rounded-md disabled:opacity-50">Save crop</button>
        <button type="button" onClick={() => void run(null)} disabled={busy}
          className="px-2 py-1 bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md disabled:opacity-50">Full frame</button>
        {status && <span role="status" className="text-[var(--brand-text-secondary)]">{status}</span>}
      </div>
    </div>
  )
}
