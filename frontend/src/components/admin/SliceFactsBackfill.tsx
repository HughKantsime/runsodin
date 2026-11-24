import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { fetchAPI } from '../../api/client'
import { useOrg } from '../../contexts/OrgContext'
import { Button, Card } from '../ui'

type Progress = { state: string; total: number; done: number; not_available: number }
const path = '/print-files/plates/backfill'
const queryKey = ['slice-facts-backfill']

export default function SliceFactsBackfill() {
  const { isAdmin } = useOrg()
  const client = useQueryClient()
  const progress = useQuery({ queryKey, queryFn: () => fetchAPI<Progress>(path), enabled: isAdmin,
    refetchInterval: query => query.state.data?.state === 'running' ? 1500 : false })
  const start = useMutation({ mutationFn: () => fetchAPI<Progress>(path, { method: 'POST' }),
    onSuccess: state => client.setQueryData(queryKey, state),
    onError: () => client.invalidateQueries({ queryKey }) })
  if (!isAdmin) return null
  const state = progress.data
  return <Card padding="lg" className="mb-4">
    <h2 className="text-lg font-semibold">Library slice facts</h2>
    <Button onClick={() => start.mutate()} disabled={start.isPending || state?.state === 'running'}>Read slice facts for older files</Button>
    {state && state.state !== 'idle' && <p role="status">{state.state === 'complete' && !state.total
      ? 'Every supported file has already been checked.' : `${state.state}: ${state.done} of ${state.total} files checked, ${state.not_available} unavailable.`}</p>}
    {(start.error || progress.error) && <p role="alert">{(start.error || progress.error)?.message}</p>}
  </Card>
}
