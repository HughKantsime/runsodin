import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { jobs } from '../../api'
import { canDo } from '../../permissions'
import AmsMappingEditor, { useAmsMapping, type AmsMapping } from '../shared/AmsMappingEditor'
import type { ArchiveAmsPreview } from '../../types/archive'

export default function JobAmsMapping({ job }: { job: {
  id: number; status: string; printer_id?: number | null; ams_mapping?: number[] | null;
  ams_preview?: ArchiveAmsPreview | null; ams_mapping_editable?: boolean;
} }) {
  const client = useQueryClient()
  const selection = useAmsMapping(job.ams_preview, `${job.id}:${job.printer_id}:${JSON.stringify(job.ams_mapping)}`, job.ams_mapping)
  const save = useMutation({ mutationFn: (mapping: AmsMapping | null) => jobs.update(job.id, { ams_mapping: mapping }),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ['job', job.id] })
      client.invalidateQueries({ queryKey: ['jobs'] })
      client.invalidateQueries({ queryKey: ['job-timeline', job.id] })
    },
  })
  const editable = canDo('jobs.edit') && job.ams_mapping_editable && ['pending', 'scheduled'].includes(job.status)
  return <section aria-label="Job AMS mapping" className="rounded-md border border-[var(--brand-card-border)] p-4">
    <p className="text-sm">Saved AMS mapping: {job.ams_mapping?.length ? job.ams_mapping.map((slot, index) => `Filament ${index + 1} → Slot ${slot + 1}`).join(', ') : 'None'}</p>
    {editable && <>
      <AmsMappingEditor preview={job.ams_preview} {...selection} />
      <button type="button" disabled={save.isPending || !selection.value} onClick={() => save.mutate(selection.value!)} className="px-3 py-2 bg-[var(--brand-primary)] rounded text-sm">Save AMS mapping</button>
      {job.ams_mapping != null && <button type="button" disabled={save.isPending} onClick={() => save.mutate(null)} className="px-3 py-2 text-sm">Clear AMS mapping</button>}
    </>}
    {save.isError && <p role="alert">{save.error.message || 'Could not update AMS mapping'}</p>}
    {save.isSuccess && <p role="status">AMS mapping saved</p>}
  </section>
}


export function JobAmsMappingPanel({ jobId }: { jobId: number }) {
  const { data, isLoading, error } = useQuery({ queryKey: ['job', jobId], queryFn: () => jobs.get(jobId) })
  if (isLoading) return <p role="status">Loading job mapping...</p>
  if (error) return <p role="alert">Could not load job mapping.</p>
  return data ? <JobAmsMapping job={data} /> : null
}
