import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { archives } from '../../api'
import { canDo } from '../../permissions'

export default function UnaccountedFilament() {
  const permitted = canDo('archives.settle_filament')
  const result = useQuery({ queryKey: ['archive-accounting-summary'], queryFn: archives.accountingSummary, enabled: permitted, retry: false })
  if (!permitted || result.isError || !result.data?.unresolved) return null
  return <p className="my-3 text-sm"><Link to="/archives?accounting=unresolved" className="underline">
    {result.data.unresolved} finished {result.data.unresolved === 1 ? 'print needs its' : 'prints need their'} filament settled
  </Link>{result.data.by_cause[0] && <> · {result.data.by_cause[0].title}</>}</p>
}
