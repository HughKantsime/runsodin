import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { auth, training, type TrainingFrame } from '../../api'
import type { User } from '../../types'
import { canDo } from '../../permissions'
import { Button, Modal } from '../ui'
import BoundedVisionImage from './BoundedVisionImage'
import { boundedBytes, imageBudget, maintenanceAudience, MiB, type Preview } from './maintenanceMedia'

export default function TrainingDataset({ initialId }: { initialId?: number }) {
  const client = useQueryClient()
  const { data: principal } = useQuery<User | null>({ queryKey: ['auth', 'me'], queryFn: auth.me, enabled: false })
  const audience = maintenanceAudience(principal)
  const [page, setPage] = useState(0), [layout, setLayout] = useState('grid'), [filter, setFilter] = useState('all')
  const [selection, setSelection] = useState<{ row: TrainingFrame; audience: string } | null>(null)
  const [image, setImage] = useState<Preview | null>(null), [previewRevision, setPreviewRevision] = useState(0)
  const [label, setLabel] = useState(''), [box, setBox] = useState(['', '', '', ''])
  const [error, setError] = useState(''), [blocked, setBlocked] = useState(false), [busy, setBusy] = useState(false), [exporting, setExporting] = useState(false)
  const generation = useRef(0), pending = useRef<number | null>(null), exportRequest = useRef<AbortController | null>(null)
  const liveAudience = useRef(audience); if (liveAudience.current !== audience) { liveAudience.current = audience; generation.current++; pending.current = null; exportRequest.current?.abort() }
  const current = (owner: string) => !!owner && maintenanceAudience(client.getQueryData<User | null>(['auth', 'me'])) === owner
  const query = useQuery({ queryKey: ['vision-training', audience, page, filter], queryFn: () => training.list(page * 25, filter === 'all' ? undefined : filter === 'labeled'), enabled: !!audience })
  const rows = query.data || []
  const selected = selection?.audience === audience ? selection.row : null
  const close = () => { generation.current++; pending.current = null; setBusy(false); setSelection(null); setImage(null); setError(''); setBlocked(false) }
  useEffect(() => { close(); setPage(0); setExporting(false); return () => { generation.current++; exportRequest.current?.abort(); imageBudget.clear(audience) } }, [audience])
  const direct = useQuery({ queryKey: ['vision-training-selection', audience, initialId], queryFn: () => training.getFrame(initialId!), enabled: !!audience && !!initialId })
  const autoOpened = useRef('')
  useEffect(() => { if (initialId && direct.data && autoOpened.current !== `${audience}:${initialId}`) { autoOpened.current = `${audience}:${initialId}`; setSelection({ row: direct.data, audience }) } }, [initialId, direct.data, audience])
  useEffect(() => { setImage(null); setLabel(selected?.annotation?.class || ''); setBox(Array.isArray(selected?.annotation?.bbox) && selected.annotation.bbox.length === 4 ? selected.annotation.bbox.map(String) : ['', '', '', '']); setBlocked(false) }, [selected?.id, selected?.annotation])
  const reload = async (owner: string, epoch: number) => {
    if (!selected || !current(owner) || generation.current !== epoch) return
    setBlocked(true)
    let result: TrainingFrame
    try { result = await client.fetchQuery({ queryKey: ['vision-training-record', owner, selected?.id], queryFn: () => training.getFrame(selected!.id), staleTime: 0 }) } catch { if (current(owner) && generation.current === epoch) setError('Authoritative reload failed. Reload before trying to save.'); return }
    if (generation.current !== epoch || !current(owner)) return
    setSelection({ row: result, audience: owner }); setPreviewRevision(value => value + 1); setBlocked(false)
  }
  const save = async () => {
    if (pending.current !== null || blocked || !selected || !image || !current(audience) || !canDo('printers.edit')) return
    const negative = label === 'false_positive'; const bbox = negative ? [] : box.map(Number)
    if (!['spaghetti', 'first_layer', 'detachment', 'build_plate_empty', 'false_positive'].includes(label) || (!negative && (box.some(value => !value.trim()) || bbox.some(value => !Number.isFinite(value)) || !(0 <= bbox[0] && bbox[0] < bbox[2] && bbox[2] <= image.width && 0 <= bbox[1] && bbox[1] < bbox[3] && bbox[3] <= image.height)))) { setError('Choose a class and valid image coordinates.'); return }
    const revision = selected.annotation?.revision ?? 0
    if (!Number.isInteger(revision) || revision < 0 || revision > 2147483646) { setError('Existing annotation revision is unsupported.'); return }
    const attempt = { owner: audience, epoch: generation.current, id: selected.id, body: { expected_revision: revision, expected_image_sha256: image.digest, class: label, negative, bbox } }
    pending.current = attempt.epoch; setBusy(true); setError('')
    try {
      if (!current(attempt.owner)) return
      await training.annotate(attempt.id, attempt.body)
      if (generation.current !== attempt.epoch || !current(attempt.owner)) return
      client.invalidateQueries({ queryKey: ['vision-training', audience] }); client.invalidateQueries({ queryKey: ['vision-detections', audience] }); close()
    } catch {
      if (generation.current !== attempt.epoch || !current(attempt.owner)) return
      setError('Save unconfirmed. Reload authoritative state and preview before editing again.'); setBlocked(true)
      try { await reload(attempt.owner, attempt.epoch) } catch { if (current(attempt.owner) && generation.current === attempt.epoch) setError('Authoritative reload failed. Reload before trying to save.') }
    } finally { if (generation.current === attempt.epoch) { pending.current = null; setBusy(false) } }
  }
  const exportData = async () => {
    if (exportRequest.current || !current(audience)) return
    const controller = new AbortController(), owner = audience; exportRequest.current = controller; setExporting(true); setError('')
    let timedOut = false
    const timer = setTimeout(() => { timedOut = true; controller.abort(); if (current(owner)) setError('Export timed out. Try again.') }, 30_000); let url = ''
    try {
      const response = await fetch('/api/vision/training-data/export', { credentials: 'include', signal: controller.signal })
      if (response.headers.get('content-type')?.split(';')[0].trim().toLowerCase() !== 'application/zip') throw new Error('Expected a ZIP dataset')
      const bytes = await boundedBytes(response, 80 * MiB, controller)
      if (controller.signal.aborted || !current(owner)) return
      url = URL.createObjectURL(new Blob([bytes], { type: 'application/zip' }))
      if (!current(owner)) return
      const link = document.createElement('a'); link.href = url; link.download = 'vision-maintenance-dataset.zip'; link.click()
    } catch (err) { if ((!controller.signal.aborted || timedOut) && current(owner)) setError(timedOut ? 'Export timed out. Try again.' : err instanceof Error ? err.message : 'Export failed') }
    finally { clearTimeout(timer); controller.abort(); if (url) URL.revokeObjectURL(url); if (exportRequest.current === controller) { exportRequest.current = null; if (current(owner)) setExporting(false) } }
  }
  if (!audience) return <p>Historical Vision datasets are available to platform maintenance only.</p>
  const annotation = selected?.annotation
  const invalidAnnotation = !!annotation && (typeof annotation !== 'object' || !Number.isInteger(annotation.revision) || annotation.revision < 1 || annotation.revision > 2147483646 || typeof annotation.class !== 'string' || !['spaghetti', 'first_layer', 'detachment', 'build_plate_empty', 'false_positive'].includes(annotation.class) || typeof annotation.negative !== 'boolean' || annotation.negative !== (annotation.class === 'false_positive') || !Array.isArray(annotation.bbox) || (annotation.negative ? annotation.bbox.length !== 0 : annotation.bbox.length !== 4 || annotation.bbox.some(value => typeof value !== 'number' || !Number.isFinite(value))) || !/^[0-9a-f]{64}$/.test(annotation.image_sha256))
  const mismatch = !!(image && !invalidAnnotation && selected?.annotation && selected.annotation.image_sha256 !== image.digest)
  return <section className="space-y-3" aria-label="Historical training dataset">
    <p>Historical owner unknown. Platform maintenance annotations only. Legacy annotations do not all have verified viewed-image provenance. Export class names do not establish trained-model accuracy.</p>
    <div className="flex gap-2"><Button onClick={() => setLayout('grid')} aria-pressed={layout === 'grid'}>Dataset grid</Button><Button onClick={() => setLayout('list')} aria-pressed={layout === 'list'}>Dataset list</Button>
      <select aria-label="Annotation filter" value={filter} onChange={event => { close(); setPage(0); setFilter(event.target.value) }}><option value="all">All annotations</option><option value="labeled">Annotated</option><option value="unlabeled">Unannotated</option></select>
      <Button onClick={() => void exportData()} loading={exporting}>Export training ZIP</Button></div>
    {direct.error && <p role="alert">Selected frame metadata unavailable. Refresh before annotating.</p>}{error && <p role="alert">{error}</p>}{query.isLoading && <p>Loading dataset...</p>}{query.error && <p role="alert">Dataset unavailable. <button onClick={() => query.refetch()}>Retry</button></p>}
    <div className={layout === 'grid' ? 'grid gap-3 sm:grid-cols-2 lg:grid-cols-3' : 'flex flex-col gap-3'}>{rows.map((row, index) => <article key={row.id} className="rounded border p-3">
      {layout === 'grid' && index < 4 && <BoundedVisionImage path={row.frame_path} audience={audience} alt="Training thumbnail" className="max-h-32" />}
      <p>Detection {row.id}: {row.detection_type}. {row.annotation ? 'Human annotation: ' + row.annotation.class : 'Unannotated'}</p><Button onClick={() => { if (current(audience)) { close(); setSelection({ row, audience }) } }}>Preview and label</Button>
    </article>)}</div>
    {!query.isLoading && rows.length === 0 && <p>No registered detection-backed frames match these filters.</p>}
    <div className="flex gap-2"><Button disabled={page === 0} onClick={() => { close(); setPage(value => value - 1) }}>Previous dataset page</Button><Button disabled={rows.length < 25} onClick={() => { close(); setPage(value => value + 1) }}>Next dataset page</Button></div>
    <Modal isOpen={!!selected} onClose={close} title="Human training annotation"><p>This edits the maintenance annotation, not the original inference or alert review.</p>
      {selected && <BoundedVisionImage key={`${selected.id}:${previewRevision}`} path={selected.frame_path} audience={audience} selected alt="Annotation preview" className="max-h-80" onImage={setImage} />}
      {image && <p>Image coordinates: {image.width} by {image.height}</p>}
      <select aria-label="Human class" value={label} disabled={busy || !canDo('printers.edit')} onChange={event => setLabel(event.target.value)}><option value="">Choose a class</option>{['spaghetti', 'first_layer', 'detachment', 'build_plate_empty', 'false_positive'].map(value => <option key={value} value={value}>{value}</option>)}</select>
      {label !== 'false_positive' && ['Left', 'Top', 'Right', 'Bottom'].map((name, index) => <label key={name}>{name}<input aria-label={name} type="number" step="any" value={box[index]} disabled={busy || !canDo('printers.edit')} onChange={event => setBox(values => values.map((value, i) => i === index ? event.target.value : value))} /></label>)}
      {invalidAnnotation && <p role="alert">Existing annotation is invalid. Maintenance repair is required.</p>}{mismatch && <p role="alert">Stored annotation refers to different bytes. Maintenance repair is required; this editor cannot rebind it.</p>}
      <Button onClick={() => void save()} loading={busy} disabled={!image || invalidAnnotation || mismatch || blocked || !canDo('printers.edit')}>Save human annotation</Button>
      {blocked && <Button onClick={() => void reload(audience, generation.current)}>Reload authoritative annotation</Button>}
    </Modal>
  </section>
}
