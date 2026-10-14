import { useRef, useState } from 'react'
import { vision } from '../../api'

export default function VisionModelUpload({ onUploaded }) {
  const file = useRef<HTMLInputElement>(null)
  const inFlight = useRef(false)
  const [format, setFormat] = useState('obico_boxes_confs')
  const [kind, setKind] = useState('spaghetti')
  const [layout, setLayout] = useState('channels_first')
  const [size, setSize] = useState(416)
  const [classes, setClasses] = useState(1)
  const [positive, setPositive] = useState('0')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const upload = async () => {
    if (inFlight.current) return
    const selected = file.current?.files?.[0]
    if (!selected) { setError('Choose an ONNX file first.'); return }
    const parts = positive.split(',').map(x => x.trim())
    const ids = parts.map(Number)
    if (!Number.isInteger(size) || size < 16 || size > 4096 || !Number.isInteger(classes)
      || classes < 1 || classes > 1000 || parts.some(x => !x)
      || ids.some(x => !Number.isInteger(x) || x < 0 || x >= classes) || new Set(ids).size !== ids.length) {
      setError('Check input size, class count and positive class IDs.'); return
    }
    const contract = { version: 1, format, layout: format === 'obico_boxes_confs' ? null : layout,
      class_count: format === 'obico_boxes_confs' ? 1 : classes,
      positive_class_ids: format === 'obico_boxes_confs' ? [0] : ids }
    inFlight.current = true
    setBusy(true); setError('')
    try {
      try {
        await vision.uploadModel(selected, selected.name.replace(/\.onnx$/i, ''), kind, size, contract)
      } catch (failure) {
        if (failure?.status === 503) {
          try { await onUploaded() } catch { /* Keep uncertainty visible. */ }
          setError('Model registration is unconfirmed. Check the model registry before uploading again.')
        } else setError('Model upload failed. Check its output contract and server diagnostics.')
        return
      }
      if (file.current) file.current.value = ''
      try { await onUploaded() }
      catch { setError('Model uploaded, but the registry could not be refreshed. Refresh model status before retrying.') }
    } finally { inFlight.current = false; setBusy(false) }
  }
  return <div className="space-y-3 text-sm">
    <label className="block">ONNX file<input className="block" ref={file} type="file" accept=".onnx" /></label>
    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
      <label>Detection type<select className="block w-full bg-[var(--brand-input-bg)]" value={kind} onChange={e => setKind(e.target.value)}>
        <option value="spaghetti">Spaghetti</option><option value="first_layer">First layer</option>
        <option value="detachment">Detachment</option><option value="build_plate_empty">Empty build plate</option>
      </select></label>
      <label>Output format<select className="block w-full bg-[var(--brand-input-bg)]" value={format} onChange={e => { setFormat(e.target.value); setSize(e.target.value === 'obico_boxes_confs' ? 416 : 640) }}>
        <option value="obico_boxes_confs">Obico boxes and confidences</option><option value="yolov8_class_scores">YOLOv8 class scores</option>
      </select></label>
      <label>Image input size<input className="block w-full bg-[var(--brand-input-bg)]" type="number" min="16" max="4096" value={size} onChange={e => setSize(Number(e.target.value))} /></label>
      {format === 'yolov8_class_scores' && <>
        <label>Output layout<select className="block w-full bg-[var(--brand-input-bg)]" value={layout} onChange={e => setLayout(e.target.value)}>
          <option value="channels_first">Features then boxes</option><option value="rows_first">Boxes then features</option>
        </select></label>
        <label>Class count<input className="block w-full bg-[var(--brand-input-bg)]" type="number" min="1" max="1000" value={classes} onChange={e => setClasses(Number(e.target.value))} /></label>
        <label>Positive class IDs<input className="block w-full bg-[var(--brand-input-bg)]" value={positive} onChange={e => setPositive(e.target.value)} /></label>
        <p className="sm:col-span-2 text-xs text-[var(--brand-text-muted)]">Use the model export's layout and class count. List the class IDs that indicate this detection, separated by commas.</p>
      </>}
    </div>
    {error && <p role="alert" className="text-red-400">{error}</p>}
    <button onClick={upload} disabled={busy} className="px-3 py-2 rounded-md bg-[var(--brand-input-bg)] disabled:opacity-50">{busy ? 'Uploading...' : 'Upload Model'}</button>
  </div>
}
