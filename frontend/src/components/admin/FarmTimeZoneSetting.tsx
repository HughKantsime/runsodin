import { useEffect, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Globe } from 'lucide-react'
import toast from 'react-hot-toast'
import { config as configApi } from '../../api'
import { Button } from '../ui'
import { browserTimeZone, timeZoneChoices } from '../../utils/farmTime'

const SOURCES: Record<string, string> = {
  quiet_hours: 'the quiet-hours time zone',
  server: "the server's TZ setting",
  utc: 'UTC, because nothing else is set',
}

/** Settings > General: the one time zone the farm's "today" is counted in. With none saved the
 *  browser's zone is offered, unsaved; nothing is stored until the admin presses the button. */
export default function FarmTimeZoneSetting({ config }: { config?: Record<string, unknown> }) {
  const queryClient = useQueryClient()
  const saved = (config?.farm_timezone as string | null | undefined) || ''
  const effective = config?.farm_timezone_effective as { zone?: string; source?: string } | undefined
  const [zone, setZone] = useState('')
  useEffect(() => { if (config) setZone(saved || browserTimeZone()) }, [config, saved])
  const save = useMutation({
    mutationFn: (value: string) => configApi.update({ farm_timezone: value }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['config'] })
      queryClient.invalidateQueries({ queryKey: ['stats'] })
      toast.success('Farm time zone saved')
    },
    onError: (err: any) => toast.error('Save failed: ' + (err?.message || 'Unknown error')),
  })
  if (!config) return null
  return (
    <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-4 md:p-6 mb-4 md:mb-6">
      <div className="flex items-center gap-2 md:gap-3 mb-4">
        <Globe size={18} className="text-[var(--brand-primary)]" />
        <h2 id="settings-farm-timezone" className="text-lg md:text-xl font-display font-semibold scroll-mt-4">Farm time zone</h2>
      </div>
      <label htmlFor="farm-timezone" className="block text-sm text-[var(--brand-text-secondary)] mb-2">
        Farm time zone: used for 'today', daily totals and quiet hours
      </label>
      <div className="flex flex-wrap items-center gap-3">
        <select id="farm-timezone" value={zone} onChange={e => setZone(e.target.value)}
          className="max-w-md bg-[var(--brand-input-bg)] text-[var(--brand-text-primary)] border border-[var(--brand-card-border)] rounded-md p-2">
          {timeZoneChoices(zone, saved, browserTimeZone()).map(z => <option key={z} value={z}>{z}</option>)}
        </select>
        <Button variant="primary" onClick={() => save.mutate(zone)} disabled={save.isPending || !zone || zone === saved}>
          {saved ? 'Save farm time zone' : 'Save to use this for the whole farm'}
        </Button>
      </div>
      <p className="text-xs text-[var(--brand-text-muted)] mt-2" data-testid="farm-timezone-status">
        {saved
          ? `"Today" and daily totals start at midnight, ${saved} time.${zone !== saved ? ' Not saved yet.' : ''}`
          : `Not set. ${zone} is your browser's time zone and is not saved yet. Until you save, "today" follows ${effective?.zone || 'UTC'}${effective?.source && SOURCES[effective.source] ? ` (${SOURCES[effective.source]})` : ''}.`}
      </p>
    </div>
  )
}
