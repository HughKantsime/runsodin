import { useEffect, useState } from 'react'
import { fetchAPI } from '../../api/client'
import { canDo } from '../../permissions'

type Preset = { preset_id: string; material: string; nozzle_temp_min: number; nozzle_temp_max: number }

export default function TraySettings({ printerId }: { printerId: number }) {
  const [presets, setPresets] = useState<Preset[]>([])
  const [open, setOpen] = useState(false)
  const [pending, setPending] = useState(false)
  const [message, setMessage] = useState('')
  const [preset, setPreset] = useState({ preset_id: '', material: '', nozzle_temp_min: '', nozzle_temp_max: '' })
  const [ams, setAms] = useState('0')
  const [slot, setSlot] = useState('0')
  const [color, setColor] = useState('#FFFFFF')
  useEffect(() => {
    let current = true
    if (open) fetchAPI<Preset[]>(`/printers/${printerId}/ams/presets`).then(value => {
      if (current) setPresets(value)
    }).catch(() => { if (current) setMessage('Reported presets unavailable; enter the printer preset details.') })
    return () => { current = false }
  }, [open, printerId])
  if (!canDo('printers.edit')) return null
  return <div className="my-3 text-sm">
    <button type="button" onClick={() => setOpen(!open)}>Configure AMS tray</button>
    {open && <form className="space-y-2 mt-2" onSubmit={async event => {
      event.preventDefault()
      if (pending || !canDo('printers.edit')) return
      setPending(true); setMessage('')
      try {
        const reply = await fetchAPI<{ result: string; reason: string | null }>(`/printers/${printerId}/ams/${ams}/slots/${slot}`, {
          method: 'PUT', body: JSON.stringify({ ...preset, color, nozzle_temp_min: Number(preset.nozzle_temp_min), nozzle_temp_max: Number(preset.nozzle_temp_max) }),
        })
        setMessage(`Printer result: ${reply.result}${reply.reason ? ` (${reply.reason})` : ''}`)
      } catch (error: any) {
        setMessage(error.detail?.reason != null
          ? `Printer result: ${error.detail.result ?? 'unconfirmed'} (${error.detail.reason})`
          : error.message || 'Tray setting not confirmed')
      }
      finally { setPending(false) }
    }}>
      <p>Use a reported preset or enter its exact details.</p>
      {presets.length > 0 && <label className="block">Reported preset<select aria-label="Reported preset" defaultValue="" onChange={event => {
        const selected = presets[Number(event.target.value)]
        if (selected) setPreset({ ...selected, nozzle_temp_min: String(selected.nozzle_temp_min), nozzle_temp_max: String(selected.nozzle_temp_max) })
      }}><option value="" disabled>Select preset</option>{presets.map((item, index) => <option key={index} value={index}>{item.preset_id} · {item.material} · {item.nozzle_temp_min}–{item.nozzle_temp_max} C</option>)}</select></label>}
      <label className="block">AMS (0–3)<input aria-label="AMS" type="number" min="0" max="3" required value={ams} onChange={event => setAms(event.target.value)} /></label>
      <label className="block">Slot (0–3)<input aria-label="Slot" type="number" min="0" max="3" required value={slot} onChange={event => setSlot(event.target.value)} /></label>
      {(['preset_id', 'material', 'nozzle_temp_min', 'nozzle_temp_max'] as const).map(key => <label className="block" key={key}>
        {{ preset_id: 'Preset ID', material: 'Material', nozzle_temp_min: 'Minimum nozzle temperature', nozzle_temp_max: 'Maximum nozzle temperature' }[key]}
        <input required aria-label={key} type={key.startsWith('nozzle') ? 'number' : 'text'} min="0" max="350" value={preset[key]} onChange={event => setPreset({ ...preset, [key]: event.target.value })} />
      </label>)}
      <label className="block">Colour<input aria-label="Colour" type="color" value={color} onChange={event => setColor(event.target.value)} /></label>
      <button disabled={pending} type="submit">{pending ? 'Waiting for printer…' : 'Set tray'}</button>
    </form>}
    {message && <p role="status">{message}</p>}
  </div>
}
