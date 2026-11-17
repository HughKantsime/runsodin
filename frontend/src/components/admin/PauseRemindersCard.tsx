import { useEffect, useState } from 'react'
import { pauseReminders } from '../../api'
import type { PauseReminderSettings } from '../../api'
import { Button } from '../ui'

export default function PauseRemindersCard({ editable }: { editable: boolean }) {
  const [settings, setSettings] = useState<PauseReminderSettings | null>(null)
  const [first, setFirst] = useState('')
  const [repeat, setRepeat] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  useEffect(() => {
    let live = true
    pauseReminders.get().then(data => {
      if (!live) return
      if (typeof data?.enabled !== 'boolean' || typeof data.first_hours !== 'number' || typeof data.repeat_hours !== 'number') {
        setError('Could not load reminder settings')
        return
      }
      setSettings(data)
      setFirst(String(data.first_hours))
      setRepeat(String(data.repeat_hours))
    }).catch(() => { if (live) setError('Could not load reminder settings') })
    return () => { live = false }
  }, [])

  const save = async () => {
    if (!settings) return
    setSaving(true)
    setSaved(false)
    setError(null)
    try {
      const updated = await pauseReminders.update({ ...settings, first_hours: first === '' ? null : Number(first),
        repeat_hours: repeat === '' ? null : Number(repeat) })
      setSettings(updated)
      setFirst(String(updated.first_hours))
      setRepeat(String(updated.repeat_hours))
      setSaved(true)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Could not save reminder settings')
    } finally {
      setSaving(false)
    }
  }

  return <div className="mt-3 pt-3 border-t border-[var(--brand-border)]">
    {settings ? <>
      <label className="flex items-center gap-2 text-sm font-medium">
        <input type="checkbox" role="switch" checked={settings.enabled} disabled={!editable || saving}
          onChange={e => { setSettings({ ...settings, enabled: e.target.checked }); setSaved(false) }} />
        Remind me when a print stays paused
      </label>
      <p className="text-xs text-[var(--brand-text-muted)] mt-2">
        These settings apply to the whole farm. Reminders use your Print Paused channels and respect quiet hours.
      </p>
      <div className="flex flex-wrap gap-3 mt-3">
        <label className="text-sm">After (hours)
          <input type="number" min="0.25" max="168" step="0.25" value={first} readOnly={!editable} disabled={saving}
            onChange={e => { setFirst(e.target.value); setSaved(false) }}
            className="block w-28 px-2 py-1 rounded-md bg-[var(--brand-surface)] border border-[var(--brand-border)]" />
        </label>
        <label className="text-sm">Then every (hours, 0 for once)
          <input type="number" min="0" max="168" step="any" value={repeat} readOnly={!editable} disabled={saving}
            onChange={e => { setRepeat(e.target.value); setSaved(false) }}
            className="block w-28 px-2 py-1 rounded-md bg-[var(--brand-surface)] border border-[var(--brand-border)]" />
        </label>
      </div>
      {editable && <Button className="mt-3" onClick={save} loading={saving}>Save pause reminders</Button>}
      {saved && <p role="status" className="text-sm mt-2">Reminder settings saved</p>}
    </> : !error && <p className="text-sm">Loading reminder settings…</p>}
    {error && <p role="alert" className="text-sm text-red-400 mt-2">{error}</p>}
  </div>
}
