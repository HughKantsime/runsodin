import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { updates, type UpdateStatus } from '../../api/system'
import { useOrg } from '../../contexts/OrgContext'
import Card from '../ui/Card'
import Button from '../ui/Button'

const disclosure = "When this is on, ODIN asks GitHub once a day for the list of ODIN releases (api.github.com/repos/HughKantsime/runsodin/releases). ODIN sends nothing about this server: no licence, no identifier, no usage data. GitHub sees this server's public IP address, as any website does. When this is off, ODIN makes no such request."

function lastResult(status: UpdateStatus) {
  if (!status.checked_at) return 'No check yet'
  if (status.outcome !== 'ok') return `Could not reach GitHub on ${new Date(status.checked_at).toLocaleString()}`
  if (!status.newest) return 'No released versions found'
  if (!status.available) return 'Up to date'
  return `Version ${status.newest} is available${status.security ? ' and fixes a security problem' : ''}`
}

export default function UpdateSettings() {
  const { isAdmin } = useOrg()
  const client = useQueryClient()
  const queryKey = ['system-updates']
  const { data, isError } = useQuery({ queryKey, queryFn: updates.get, enabled: isAdmin, refetchInterval: 60000 })
  const change = useMutation({
    mutationFn: (action: boolean | 'check') => action === 'check' ? updates.check() : updates.configure(action),
    onSuccess: (status) => {
      client.setQueryData(queryKey, status)
      client.invalidateQueries({ queryKey: ['system-notices'] })
    },
  })
  if (!isAdmin) return null
  return (
    <Card padding="lg" className="mb-4 md:mb-6">
      <h2 className="text-lg font-display font-semibold mb-2">Updates</h2>
      <label className="flex items-center gap-2">
        <input type="checkbox" role="switch" checked={data?.enabled ?? false}
          disabled={!data || !!data.disabled_reason || change.isPending}
          onChange={event => change.mutate(event.target.checked)} />
        Check for new ODIN versions
      </label>
      <p className="text-sm mt-2">{disclosure}</p>
      {data?.disabled_reason && <p className="text-sm mt-2">{data.disabled_reason}</p>}
      {isError && <p role="alert">Could not load update settings.</p>}
      {change.isError && <p role="alert">{change.error.message}</p>}
      {data && (data.enabled ? (
        <div className="text-sm space-y-2 mt-3">
          <p>Installed version: {data.installed}</p>
          <p>Newest released version: {data.newest ?? 'Unknown'}</p>
          {data.published_at && <p>Released: {new Date(data.published_at).toLocaleDateString()}</p>}
          <p>Last checked: {data.checked_at ? new Date(data.checked_at).toLocaleString() : 'Never'}</p>
          <p role="status">{lastResult(data)}</p>
          <Button variant="secondary" disabled={change.isPending} onClick={() => change.mutate('check')}>Check now</Button>
        </div>
      ) : (
        <p className="text-sm mt-2">Off. ODIN does not check for updates. To hear about releases without this, watch the repository's releases and security advisories on GitHub.</p>
      ))}
    </Card>
  )
}
