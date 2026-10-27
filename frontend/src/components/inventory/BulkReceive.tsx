import { useState } from 'react'
import { useQuery, useQueryClient, useMutation } from '@tanstack/react-query'
import { filaments, spools } from '../../api/inventory'
import { Button, Modal } from '../ui'
import { csvExample, previewCsv, type PreviewRow } from './receivingCsv'

export default function BulkReceive({ onClose, onReceived }: { onClose: () => void; onReceived: (ids: number[]) => void }) {
  const client = useQueryClient()
  const [csv, setCsv] = useState(''), [error, setError] = useState('')
  const [importId, setImportId] = useState<string>(() => crypto.randomUUID())
  const [preview, setPreview] = useState<PreviewRow[] | null>(null)
  const library = useQuery({ queryKey: ['filaments', 'receiving'], queryFn: filaments.list })
  const receive = useMutation({ mutationFn: spools.receiveBulk, retry: false,
    onSuccess: result => { client.invalidateQueries({ queryKey: ['spools'] }); client.invalidateQueries({ queryKey: ['filaments'] }); onReceived(result.spools.map(s => s.id)) } })
  const change = (text: string) => { setCsv(text); setPreview(null); setError(''); receive.reset() }
  return <Modal isOpen title="Bulk receive rolls" onClose={() => { if (!receive.isPending) onClose() }}>
    <div className="space-y-3">
      <p>Paste or upload CSV. Review every row, then confirm. Missing product lines are created only on confirmation. Use material PLA or PETG and the product line PLA Silk or PETG HF.</p>
      <label className="block">Import ID<input aria-label="Import ID" value={importId} disabled={receive.isPending} onChange={e => { setImportId(e.target.value); receive.reset() }} /></label>
      <p className="text-sm">Keep this ID to safely retry this exact import: {importId}</p>
      <label className="block">CSV file<input aria-label="CSV file" type="file" accept=".csv,text/csv" disabled={receive.isPending} onChange={async e => {
        const file = e.target.files?.[0]; if (!file) return
        if (file.size > 1000000) { setError('CSV file is too large (maximum 1 MB).'); return }
        try { change(await file.text()) } catch { setError('Could not read CSV file.') }
      }} /></label>
      <label className="block">CSV rows<textarea aria-label="CSV rows" rows={6} className="w-full bg-[var(--brand-input-bg)]" value={csv} disabled={receive.isPending} onChange={e => change(e.target.value)} /></label>
      <pre className="text-xs whitespace-pre-wrap">{csvExample}</pre>
      <p className="text-xs">Optional columns: color_hex, initial_weight_g, spool_weight_g, sealed (true/false), vendor, note.</p>
      {library.isError && <p role="alert">Could not load product lines. <button onClick={() => library.refetch()}>Retry</button></p>}
      <Button disabled={!library.data || receive.isPending} onClick={() => { try { setPreview(previewCsv(csv, library.data!)); setError(''); receive.reset() } catch (e) { setPreview(null); setError((e as Error).message) } }}>Preview rows</Button>
      {preview && <div className="overflow-x-auto"><table className="w-full text-sm"><thead><tr>{['Product line', 'Material', 'Color', 'Rolls', 'Grams / tare', 'Location / seal', 'Vendor / note', 'Review'].map(h => <th key={h}>{h}</th>)}</tr></thead><tbody>
        {preview.map(({ row, errors, newProduct }, i) => <tr key={i}>
          <td>{row.brand} {row.product_line}</td><td>{row.material}</td><td>{row.color_name || 'Unknown color'} {row.color_hex}</td><td>{row.quantity}</td><td>{row.initial_weight_g} / {row.spool_weight_g}</td><td>{row.storage_location} / {row.sealed ? 'sealed' : 'open'}</td><td>{row.vendor} {row.note}</td>
          <td>{newProduct ? 'New product line' : 'Existing product line'}{errors.map(e => <p role="alert" key={e}>{e}</p>)}</td></tr>)}
      </tbody></table></div>}
      {(error || receive.isError) && <p role="alert">{error || receive.error?.message}</p>}
      {preview && <Button disabled={receive.isPending || preview.some(r => r.errors.length) || !/^[A-Za-z0-9_-]{1,100}$/.test(importId)} onClick={() => receive.mutate({ import_id: importId, rows: preview.map(r => r.row) })}>Confirm receipt</Button>}
    </div>
  </Modal>
}
