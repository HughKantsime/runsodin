import { useEffect, useState } from 'react'
import { Thermometer, Save, CheckCircle, AlertTriangle } from 'lucide-react'
import { printers, temperatureAlerts } from '../../api'
import type { TemperatureAlertSettings } from '../../api'
import { Button, Card } from '../ui'

type NumericKey = 'under_c' | 'over_c' | 'sustain_minutes' | 'warmup_minutes' | 'idle_hot_nozzle_c' | 'idle_hot_bed_c'
type PrinterRow = { id: number; name: string; nickname?: string | null }

const FIELDS: { key: NumericKey; label: string; unit: string; help: string }[] = [
  { key: 'under_c', label: 'Not reaching target', unit: 'C below', help: 'Alert when a heater stays this far below its target.' },
  { key: 'over_c', label: 'Overshoot', unit: 'C above', help: 'Alert when a heater stays this far above its target.' },
  { key: 'sustain_minutes', label: 'Sustained for', unit: 'minutes', help: 'How long a reading must stay wrong, so one bad reading never alerts.' },
  { key: 'warmup_minutes', label: 'Warm-up grace', unit: 'minutes', help: 'Ignore heating after a print starts or a target changes.' },
  { key: 'idle_hot_nozzle_c', label: 'Nozzle hot with no target', unit: 'C', help: 'Alert when a nozzle with no target heats up past this.' },
  { key: 'idle_hot_bed_c', label: 'Bed hot with no target', unit: 'C', help: 'Alert when a bed with no target heats up past this.' },
]

const isSettings = (value: unknown): value is TemperatureAlertSettings =>
  !!value && typeof value === 'object' && typeof (value as TemperatureAlertSettings).enabled === 'boolean'

export default function TemperatureAlertsCard() {
  const [settings, setSettings] = useState<TemperatureAlertSettings | null>(null)
  const [printerList, setPrinterList] = useState<PrinterRow[]>([])
  const [text, setText] = useState<Partial<Record<NumericKey, string>>>({})
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    temperatureAlerts.get().then(data => { if (live && isSettings(data)) setSettings(data) }).catch(() => {})
    printers.list(true).then(list => { if (live && Array.isArray(list)) setPrinterList(list) }).catch(() => {})
    return () => { live = false }
  }, [])

  // Shown only once the settings have loaded (and only to platform admins, who the server allows).
  if (!settings) return null

  const disabled = settings.disabled_printer_ids || []
  const toggleEnabled = () => setSettings({ ...settings, enabled: !settings.enabled })
  const togglePrinter = (id: number) => setSettings({
    ...settings,
    disabled_printer_ids: disabled.includes(id) ? disabled.filter(p => p !== id) : [...disabled, id].sort((a, b) => a - b),
  })

  const save = async () => {
    setError(null)
    setSaved(false)
    const payload: TemperatureAlertSettings = { ...settings }
    for (const field of FIELDS) {
      if (text[field.key] === undefined) continue
      const value = Number(text[field.key])
      if (text[field.key] === '' || !Number.isFinite(value)) {
        setError(`Enter a number for "${field.label}".`)
        return
      }
      payload[field.key] = value
    }
    setSaving(true)
    try {
      const stored = await temperatureAlerts.update(payload)
      setSettings(isSettings(stored) ? stored : payload)
      setText({})
      setSaved(true)
      setTimeout(() => setSaved(false), 3000)
    } catch (err: any) {
      setError(err?.message || 'Failed to save temperature alert settings')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card padding="lg" className="mb-4 md:mb-6">
      <div className="flex items-center gap-2 md:gap-3 mb-4">
        <Thermometer size={18} className="text-[var(--brand-primary)]" />
        <h2 className="text-lg md:text-xl font-display font-semibold">Temperature anomaly alerts</h2>
        <button
          type="button"
          role="switch"
          aria-checked={settings.enabled}
          aria-label="Temperature anomaly alerts"
          onClick={toggleEnabled}
          className={`ml-auto w-10 h-5 rounded-full transition-colors relative ${settings.enabled ? 'bg-[var(--brand-primary)]' : 'bg-[var(--brand-surface)]'}`}
        >
          <span className={`absolute top-0.5 w-4 h-4 rounded-full bg-white transition-transform ${settings.enabled ? 'left-5' : 'left-0.5'}`} />
        </button>
      </div>
      <p className="text-sm text-[var(--brand-text-secondary)] mb-4">
        Warns when a nozzle or bed will not reach its target, overshoots it, heats with no target, or reads impossible values during a print.
        It only alerts: it never pauses or stops a print. Recipients and quiet hours follow the printer error alert settings.
      </p>

      <div className={`grid grid-cols-1 md:grid-cols-2 gap-3 ${!settings.enabled ? 'opacity-50' : ''}`}>
        {FIELDS.map(field => (
          <label key={field.key} className="block bg-[var(--brand-card-bg)] rounded-md p-3">
            <span className="text-sm font-medium">{field.label}</span>
            <span className="flex items-center gap-2 mt-1">
              <input
                aria-label={field.label}
                type="number"
                inputMode="decimal"
                value={text[field.key] ?? String(settings[field.key])}
                onChange={e => setText({ ...text, [field.key]: e.target.value })}
                className="w-24 px-2 py-1 rounded-md bg-[var(--brand-surface)] border border-[var(--brand-border)] text-sm"
              />
              <span className="text-xs text-[var(--brand-text-muted)]">{field.unit}</span>
            </span>
            <span className="block text-xs text-[var(--brand-text-muted)] mt-1">{field.help}</span>
          </label>
        ))}
      </div>

      {printerList.length > 0 && (
        <div className="mt-4">
          <h3 className="text-sm font-medium mb-2">Check these printers</h3>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
            {printerList.map(printer => {
              const label = printer.nickname || printer.name
              return (
                <label key={printer.id} className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    aria-label={`Temperature alerts for ${label}`}
                    checked={!disabled.includes(printer.id)}
                    onChange={() => togglePrinter(printer.id)}
                  />
                  {label}
                </label>
              )
            })}
          </div>
        </div>
      )}

      <div className="flex items-center gap-4 mt-4">
        <Button variant="primary" icon={Save} loading={saving} onClick={save}>Save temperature alerts</Button>
        {saved && <span className="flex items-center gap-1 text-green-400 text-sm"><CheckCircle size={14} /> Saved</span>}
        {error && <span className="flex items-center gap-1 text-red-400 text-sm"><AlertTriangle size={14} /> {error}</span>}
      </div>
    </Card>
  )
}
