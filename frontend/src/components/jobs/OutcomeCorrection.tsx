import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { fetchAPI } from '../../api/client'
import { canDo } from '../../permissions'

type Props = { jobId: number; printId?: number; status: string; onCorrected?: () => void }

export default function OutcomeCorrection(props: Props) {
  if (!props.jobId || !['failed', 'cancelled'].includes(props.status) || !canDo('jobs.edit')) return null
  return <CorrectionControl {...props} />
}

function CorrectionControl({ jobId, printId, status, onCorrected }: Props) {
  const client = useQueryClient()
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const target = status === 'failed' ? 'cancelled' : 'failed'
  return <span>
    <button type="button" disabled={pending} onClick={async () => {
      if (pending || !canDo('jobs.edit')) return
      setPending(true); setError('')
      try {
        await fetchAPI(`/jobs/${jobId}/outcome`, { method: 'POST', body: JSON.stringify({ status: target, from_status: status, print_id: printId }) })
        for (const key of ['jobs', 'archives', 'print-jobs', 'recent-terminal-jobs']) void client.invalidateQueries({ queryKey: [key] })
        onCorrected?.()
      } catch (failure: any) { setError(failure.message || 'Outcome correction failed') }
      finally { setPending(false) }
    }}>{pending ? 'Saving outcome…' : `Correct outcome to ${target}`}</button>
    {error && <span role="alert">{error}</span>}
  </span>
}
