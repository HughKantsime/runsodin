import { useMutation } from '@tanstack/react-query'
import { fetchAPI } from '../../api/client'
type Preview = { files: number[]; jobs: number[]; tombstones: { job_id: number; audit_max_id: number }[]; due_count: number; due_timelapse_ids: number[] }
export default function ControlledRetention() {
  const preview = useMutation({ mutationFn: () => fetchAPI<Preview>('/controlled-parts/retention/preview') })
  const confirm = useMutation({ mutationFn: () => fetchAPI('/controlled-parts/retention/acknowledge', {
    method: 'POST', headers: { 'X-ODIN-Controlled-Ack': 'true' }, body: JSON.stringify({ files: preview.data?.files ?? [], jobs: preview.data?.jobs ?? [], tombstones: preview.data?.tombstones ?? [] }),
  }) })
  return <div>
    <button type="button" onClick={() => { confirm.reset(); preview.mutate() }}>Preview timelapse cleanup</button>
    {preview.data && <div><p>{preview.data.due_count} timelapses are due. Cleanup starts after acknowledgement and applies to future expirations of these policies.</p>
      <button type="button" disabled={confirm.isPending || confirm.isSuccess} onClick={() => confirm.mutate()}>Acknowledge and enable cleanup</button></div>}
    {confirm.isSuccess && <p role="status">Cleanup enabled</p>}
    {(preview.isError || confirm.isError) && <p role="alert">{preview.error?.message || confirm.error?.message}</p>}
  </div>
}
