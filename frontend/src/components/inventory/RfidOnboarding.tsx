import { canDo, getCurrentUser } from '../../permissions'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { spools } from '../../api/inventory'
import { useOrg } from '../../contexts/OrgContext'

export default function RfidOnboarding() {
  const { orgId } = useOrg()
  const client = useQueryClient()
  const canMerge = canDo('spools.edit') && ['admin', 'operator'].includes(getCurrentUser()?.role || '')
  const merge = useMutation({
    mutationFn: ({ source, target }: { source: number; target: number }) => spools.mergeRfidRoll(source, target),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ['rfid-onboarding'] })
      client.invalidateQueries({ queryKey: ['spools'] })
      client.invalidateQueries({ queryKey: ['filament-slots'] })
    },
  })
  const { data: rolls = [], isLoading, isError, refetch } = useQuery({
    queryKey: ['rfid-onboarding', orgId], queryFn: () => spools.rfidOnboarding(orgId), refetchInterval: 3000,
  })
  return <section aria-label="Opened Bambu rolls" className="space-y-4">
    <h2 className="text-lg font-semibold">Opened Bambu rolls</h2>
    <p>Load each opened Bambu roll into any AMS slot, one at a time. Wait for it to appear here before loading the next roll. ODIN remembers rolls after you unload them.</p>
    <p role="status">{isLoading ? 'Checking for rolls…' : `${rolls.length} ${rolls.length === 1 ? 'roll' : 'rolls'} found`}</p>
    {isError && <p role="alert">Could not refresh the checklist. <button className="underline" onClick={() => refetch()}>Retry</button></p>}
    {!isLoading && !isError && rolls.length === 0 && <p>No RFID rolls found yet. Leave the first roll loaded until the printer reports it.</p>}
    {merge.isError && <p role="alert">{merge.error.message || 'Could not merge this roll. Refresh and try again.'}</p>}
    <ul className="space-y-3">
      {rolls.map(roll => <li key={roll.spool_id} className="rounded-lg border border-[var(--border-default)] p-4">
        <span aria-label="Recorded">✓ </span>
        <strong>Spool #{roll.spool_id} · {roll.filament_name} · {roll.color_name || 'Color not named'}</strong>
        <p>{({ bound: 'Bound to existing inventory', created: 'Created in inventory', identified: 'Identified in inventory', merged: 'Merged into sealed inventory' })[roll.outcome]} · Printer #{roll.printer_id}, slot {roll.slot}</p>
        {roll.possible_duplicate && <p className="text-amber-500">Possible duplicate: multiple sealed colors matched this line. Choose the sealed roll this RFID belongs to.</p>}
        {canMerge && roll.possible_duplicate && (roll.possible_duplicate_spool_ids || []).map(id =>
          <button key={id} disabled={merge.isPending} className="mr-2 mt-2 rounded border px-3 py-2"
            onClick={() => merge.mutate({ source: roll.spool_id, target: id })}>Use sealed #{id}</button>)}
      </li>)}
    </ul>
  </section>
}
