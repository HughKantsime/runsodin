import { useEffect, useState } from 'react'
import { fetchAPI } from '../../api/client'
import { Button, Card } from '../ui'

interface ZoneProps {
  zone: string
  confirmed: boolean
  confirm: () => void
  pending?: boolean
  saving?: boolean
}

export function AlertQuietHoursZone({ zone, confirmed, confirm, pending, saving }: ZoneProps) {
  return <div className="text-sm space-y-1">
    <p>{pending ? `Save settings to use ${zone} for alert quiet hours.` : `Alert quiet hours use ${confirmed ? zone : 'UTC'}.`}</p>
    {!confirmed && <>
      <p>These hours are still read as UTC. Confirm your time zone to apply them in local time. After confirmation, these hours will use {zone}.</p>
      <Button type="button" variant="tertiary" size="sm" disabled={saving} onClick={confirm}>
        Confirm time zone ({zone})
      </Button>
    </>}
  </div>
}

interface QuietHours {
  enabled: boolean
  start: string
  end: string
  timezone_confirmed: boolean
  local_timezone: string
}

export function FarmAlertQuietHours() {
  const [hours, setHours] = useState<QuietHours | null>(null)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  useEffect(() => {
    let active = true
    fetchAPI<QuietHours>('/config/quiet-hours').then(data => {
      if (active) setHours(data)
    }).catch(() => { if (active) setError('Could not load alert quiet hours.') })
    return () => { active = false }
  }, [])

  const confirm = async () => {
    setSaving(true)
    setError('')
    try {
      await fetchAPI('/config/quiet-hours', {
        method: 'PUT', body: JSON.stringify({ timezone_confirmed: true }),
      })
      setHours(await fetchAPI<QuietHours>('/config/quiet-hours'))
    } catch {
      setError('Could not confirm the time zone. Try again.')
    } finally {
      setSaving(false)
    }
  }
  return <Card padding="lg" className="mb-4 md:mb-6 space-y-2">
    <h2 className="text-lg font-display font-semibold">Farm alert quiet hours</h2>
    {error && <p role="alert">{error}</p>}
    {hours && (hours.enabled ? <>
      <p>{hours.start} to {hours.end}</p>
      <AlertQuietHoursZone zone={hours.local_timezone} confirmed={hours.timezone_confirmed}
        confirm={confirm} saving={saving} />
    </> : <p>Farm alert quiet hours are off.</p>)}
  </Card>
}
