import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Layers, X, Image as ImageIcon } from 'lucide-react'
import { models } from '../../api/models'
import { printFilePlates, type PlateFacts } from '../../api/printFilePlates'
import { usePlateThumbnail } from '../../hooks/usePlateThumbnail'

const SAFE_COLOR = /^#[0-9a-f]{6}([0-9a-f]{2})?$/i

export function formatPlateTime(seconds: number | null): string {
  if (seconds == null) return '—'
  if (seconds < 60) return `${seconds}s`
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  return hours ? `${hours}h ${minutes}m` : `${minutes}m`
}

const grams = (value: number | null) => (value == null ? '—' : `${value.toFixed(1)} g`)

function PlateThumb({ fileId, plate, enabled }: { fileId: number; plate: number; enabled: boolean }) {
  const { url, loading } = usePlateThumbnail(fileId, plate, enabled)
  return (
    <div className="w-full h-40 sm:w-32 sm:h-32 shrink-0 bg-[var(--brand-content-bg)] rounded-md flex items-center justify-center overflow-hidden">
      {url ? <img src={url} alt={`Plate ${plate} preview`} className="h-full w-full object-contain" />
        : loading ? <span className="text-xs text-[var(--brand-muted)]">Loading picture…</span>
        : <span className="flex flex-col items-center text-xs text-[var(--brand-muted)]"><ImageIcon size={20} />No picture</span>}
    </div>
  )
}

function PlateCard({ fileId, plate }: { fileId: number; plate: PlateFacts }) {
  return (
    <li className="flex flex-col sm:flex-row gap-3 p-3 bg-[var(--brand-input-bg)] rounded-md border border-[var(--brand-card-border)]" aria-label={`Plate ${plate.index}`}>
      <PlateThumb fileId={fileId} plate={plate.index} enabled={plate.has_thumbnail} />
      <div className="min-w-0 flex-1 text-sm space-y-2">
        <h4 className="font-medium">Plate {plate.index}</h4>
        <dl className="grid grid-cols-3 gap-2 text-xs">
          <div><dt className="text-[var(--brand-muted)]">Print time</dt><dd>{formatPlateTime(plate.print_time_seconds)}</dd></div>
          <div><dt className="text-[var(--brand-muted)]">Weight</dt><dd>{grams(plate.weight_grams)}</dd></div>
          <div><dt className="text-[var(--brand-muted)]">Layer height</dt><dd>{plate.layer_height == null ? '—' : `${plate.layer_height} mm`}</dd></div>
        </dl>
        <div>
          <p className="text-xs text-[var(--brand-muted)] mb-1">Filaments</p>
          {plate.filaments.length === 0 ? <p className="text-xs">None recorded</p> : (
            <ul className="space-y-1">
              {plate.filaments.map(f => (
                <li key={f.slot} className="flex items-center gap-2 text-xs">
                  <span className="inline-block w-4 h-4 rounded-full border border-[var(--brand-card-border)] shrink-0" data-testid="filament-swatch"
                    style={f.color && SAFE_COLOR.test(f.color) ? { backgroundColor: f.color } : undefined} title={f.color || 'No colour'} />
                  <span>{f.type}</span>
                  <span className="text-[var(--brand-muted)]">{grams(f.grams)}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="text-xs">
          {plate.warnings === null ? <p className="text-[var(--brand-muted)]">Slicer warnings: not available</p>
            : plate.warnings.length === 0 ? <p className="text-[var(--brand-muted)]">Slicer warnings: none recorded</p>
            : (
              <div>
                <p className="text-amber-400">Slicer warnings</p>
                <ul className="list-disc pl-4">{plate.warnings.map((w, i) => <li key={i}>{[w.level, w.message || w.code].filter(Boolean).join(': ') || 'Warning'}</li>)}</ul>
              </div>
            )}
        </div>
      </div>
    </li>
  )
}

export default function ModelPlatesPanel({ modelId, modelName, onClose }: { modelId: number; modelName?: string; onClose: () => void }) {
  const [chosen, setChosen] = useState<number | null>(null)
  const variantsQuery = useQuery({ queryKey: ['model-variants', modelId], queryFn: () => models.getVariants(modelId), enabled: !!modelId })
  const variants = variantsQuery.data?.variants ?? []
  const fileId = chosen ?? variants[0]?.id ?? null
  const platesQuery = useQuery({
    queryKey: ['print-file-plates', fileId],
    queryFn: ({ signal }) => printFilePlates.facts(fileId as number, signal),
    enabled: fileId != null,
    retry: false,
  })
  const status = (platesQuery.error as { status?: number } | null)?.status
  const slicer = platesQuery.data?.slicer
  const slicerText = slicer && (slicer.name || slicer.version) ? [slicer.name, slicer.version].filter(Boolean).join(' ') : null

  let body
  if (variantsQuery.isLoading) body = <p role="status" className="text-sm text-[var(--brand-muted)]">Loading plates…</p>
  else if (variantsQuery.isError) body = <p role="alert" className="text-sm text-red-400">Could not load this model's files.</p>
  else if (fileId == null) body = <p className="text-sm text-[var(--brand-muted)]">This model has no print file, so there are no plates to show.</p>
  else if (platesQuery.isLoading) body = <p role="status" className="text-sm text-[var(--brand-muted)]">Loading plates…</p>
  else if (platesQuery.isError) {
    body = status === 404
      ? <p className="text-sm text-[var(--brand-muted)]">Plate details are not available for this file (not a sliced 3MF, or the file is no longer stored).</p>
      : status === 429
        ? <div role="alert" className="text-sm"><p className="text-amber-400 mb-2">3MF analysis is busy. Try again shortly.</p><button className="px-3 py-1.5 bg-[var(--brand-primary)] rounded-md text-sm" onClick={() => platesQuery.refetch()}>Retry</button></div>
        : <p role="alert" className="text-sm text-red-400">Could not load plate details.</p>
  } else if (!platesQuery.data || platesQuery.data.plates.length === 0) body = <p className="text-sm text-[var(--brand-muted)]">This file has no plates.</p>
  else body = (
    <>
      <p className="text-xs text-[var(--brand-muted)] mb-3">{slicerText ? `Sliced with ${slicerText}` : 'Slicer not recorded'} · {platesQuery.data.plates.length} {platesQuery.data.plates.length === 1 ? 'plate' : 'plates'}</p>
      <ul className="space-y-3">{platesQuery.data.plates.map(p => <PlateCard key={p.index} fileId={fileId} plate={p} />)}</ul>
    </>
  )

  return (
    <div className="fixed inset-0 bg-black/50 flex items-end sm:items-center justify-center z-50 p-0 sm:p-4" role="dialog" aria-modal="true" aria-labelledby="plates-panel-title">
      <div className="bg-[var(--brand-card-bg)] rounded-t-xl sm:rounded w-full max-w-2xl p-4 sm:p-6 border border-[var(--brand-card-border)] max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-4 gap-2">
          <div className="flex items-center gap-2 min-w-0">
            <Layers size={18} className="text-[var(--brand-primary)] shrink-0" />
            <h2 id="plates-panel-title" className="text-lg font-display font-semibold">Plates</h2>
            <span className="text-sm text-[var(--brand-muted)] truncate">— {modelName}</span>
          </div>
          <button onClick={onClose} className="text-[var(--brand-muted)] hover:text-[var(--brand-text-secondary)]" aria-label="Close plates"><X size={20} /></button>
        </div>
        {variants.length > 1 && (
          <label className="block mb-3 text-xs text-[var(--brand-muted)]">Print file
            <select aria-label="Print file" value={fileId ?? ''} onChange={e => setChosen(Number(e.target.value))}
              className="mt-1 w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-sm text-[var(--brand-text-secondary)]">
              {variants.map(v => <option key={v.id} value={v.id}>{v.filename} ({v.printer_model})</option>)}
            </select>
          </label>
        )}
        {body}
      </div>
    </div>
  )
}
