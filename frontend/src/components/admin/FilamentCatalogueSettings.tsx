import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { filaments } from '../../api/inventory'
import { getCurrentUser } from '../../permissions'
import { Button } from '../ui'

export default function FilamentCatalogueSettings() {
  const principal = getCurrentUser()
  const client = useQueryClient()
  const [file, setFile] = useState<File | null>(null)
  const [error, setError] = useState('')
  const mutation = useMutation({ mutationFn: filaments.importBambu, onSuccess: () => {
    client.invalidateQueries({ queryKey: ['filaments'] })
    client.invalidateQueries({ queryKey: ['printers'] })
    client.invalidateQueries({ queryKey: ['spools'] })
  } })
  if (principal?.role !== 'admin' || principal?.group_id) return <p>Ask an installation administrator to import the shared filament catalogue.</p>
  return <section className="max-w-2xl min-w-0 space-y-4">
    <h2 className="text-lg font-semibold">Import Bambu catalogue</h2>
    <p>Choose the file filaments_color_codes.json from your own Bambu Studio installation (in Bambu Studio's system folder, under BBL/filament). ODIN does not include Bambu Lab's catalogue; it reads the copy you provide.</p>
    <p>The catalogue is shared by all organisations on this installation. Importing again updates names and colours without removing entries.</p>
    <label className="block">Catalogue file
      <input type="file" accept=".json,application/json" className="block w-full min-w-0 min-h-11 mt-1 text-sm text-[var(--brand-text-secondary)] file:min-h-11 file:mr-3 file:px-4 file:rounded-md file:border file:border-[var(--brand-card-border)] file:bg-[var(--brand-input-bg)] file:text-[var(--brand-text-secondary)] file:cursor-pointer" onChange={e => { setFile(e.target.files?.[0] || null); setError(''); mutation.reset() }} />
    </label>
    <Button className="min-h-11" disabled={!file || mutation.isPending} onClick={() => {
      if (!file) return
      if (file.size > 1024 * 1024) { setError('Choose a catalogue file smaller than 1 MB.'); return }
      setError(''); mutation.mutate(file)
    }}>{mutation.isPending ? 'Importing…' : 'Import Bambu catalogue'}</Button>
    {(error || mutation.error) && <p role="alert">{error || mutation.error?.message}</p>}
    {mutation.data && <div role="status" className="break-words">
      <p>{mutation.data.created} {mutation.data.created === 1 ? 'entry' : 'entries'} added; {mutation.data.updated} {mutation.data.updated === 1 ? 'entry' : 'entries'} updated.</p>
      {!!mutation.data.skipped?.length && <p>Skipped because the material type changed: {mutation.data.skipped.map(entry => entry.catalogue_code).join(', ')}. Existing entries were left unchanged.</p>}
      {mutation.data.preserved > (mutation.data.skipped?.length || 0) && <p>Existing material types were kept for {mutation.data.preserved - (mutation.data.skipped?.length || 0)} {mutation.data.preserved - (mutation.data.skipped?.length || 0) === 1 ? 'entry' : 'entries'}.</p>}
      {mutation.data.unrecognised_types.length > 0 && <p>These types can be displayed but cannot pass material checks: {mutation.data.unrecognised_types.join(', ')}.</p>}
    </div>}
  </section>
}
