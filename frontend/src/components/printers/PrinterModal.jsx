import { useState, useEffect, useRef } from 'react'
import { X, Lock } from 'lucide-react'
import clsx from 'clsx'
import { printers } from '../../api'
import { canDo } from '../../permissions'

const KNOWN_PRINTER_BEDS = {
  'x1 carbon': [256, 256],
  'x1c': [256, 256],
  'x1e': [256, 256],
  'x1': [256, 256],
  'p1s': [256, 256],
  'p1p': [256, 256],
  'a1 mini': [180, 180],
  'a1': [256, 256],
  'h2d': [320, 320],
  'mk4': [250, 210],
  'mk3': [250, 210],
  'mini': [180, 180],
  'ender 3': [220, 220],
  'ender-3': [220, 220],
  'voron': [300, 300],
}

function lookupBedSize(modelStr) {
  if (!modelStr) return null
  const lower = modelStr.toLowerCase()
  for (const [key, dims] of Object.entries(KNOWN_PRINTER_BEDS)) {
    if (lower.includes(key)) return dims
  }
  return null
}

export default function PrinterModal({ isOpen, onClose, onSubmit, printer, onSyncAms }) {
  const [formData, setFormData] = useState({
    name: '',
    model: '',
    slot_count: 4,
    api_type: '',
    api_host: '',
    serial: '',
    access_code: '',
    bed_x_mm: '',
    bed_y_mm: '',
  })
  const [testStatus, setTestStatus] = useState(null)
  const [testMessage, setTestMessage] = useState('')
  const [modelOverride, setModelOverride] = useState(false)
  const [slotOverride, setSlotOverride] = useState(false)
  const modalRef = useRef(null)

  const BAMBU_MODELS = ['X1C', 'X1E', 'X1', 'P1S', 'P1P', 'A1', 'A1 Mini', 'H2D']

  useEffect(() => {
    if (!isOpen) return
    const handleKey = (e) => {
      if (e.key === 'Escape') onClose()
      if (e.key === 'Tab' && modalRef.current) {
        const focusable = modalRef.current.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')
        if (focusable.length === 0) return
        const first = focusable[0]
        const last = focusable[focusable.length - 1]
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault()
          last.focus()
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault()
          first.focus()
        }
      }
    }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
  }, [isOpen, onClose])

  useEffect(() => {
    if (printer) {
      let serial = ''
      let access_code = ''
      if (printer.api_key && printer.api_key.includes('|')) {
        const parts = printer.api_key.split('|')
        serial = parts[0] || ''
        access_code = parts[1] || ''
      }
      setFormData({
        name: printer.name || '',
        nickname: printer.nickname || '',
        model: printer.model || '',
        slot_count: printer.slot_count || 4,
        api_type: printer.api_type || '',
        api_host: printer.api_host || '',
        serial: serial,
        access_code: access_code,
        camera_url: printer.camera_url || '',
        plug_type: printer.plug_type || '',
        plug_host: printer.plug_host || '',
        plug_topic: printer.plug_topic || '',
        plug_entity: printer.plug_entity || '',
        plug_token: printer.plug_token || '',
        tags: printer.tags || [],
        timelapse_enabled: printer.timelapse_enabled || false,
        shared: printer.shared || false,
        bed_x_mm: printer.bed_x_mm != null ? String(printer.bed_x_mm) : '',
        bed_y_mm: printer.bed_y_mm != null ? String(printer.bed_y_mm) : '',
      })
    } else {
      setFormData({ name: '', nickname: '', model: '', slot_count: 4, api_type: '', api_host: '', serial: '', access_code: '', camera_url: '', plug_type: '', plug_host: '', plug_topic: '', plug_entity: '', plug_token: '', tags: [], timelapse_enabled: false, shared: false, bed_x_mm: '', bed_y_mm: '' })
    }
    setTestStatus(null)
    setTestMessage('')
    setModelOverride(false)
    setSlotOverride(false)
  }, [printer, isOpen])

  // Auto-fill bed size from known printer model lookup
  useEffect(() => {
    if (!formData.model) return
    if (formData.bed_x_mm !== '' || formData.bed_y_mm !== '') return
    const dims = lookupBedSize(formData.model)
    if (dims) {
      setFormData(prev => ({ ...prev, bed_x_mm: String(dims[0]), bed_y_mm: String(dims[1]) }))
    }
  }, [formData.model]) // eslint-disable-line react-hooks/exhaustive-deps

  const handleTestConnection = async () => {
    if (!formData.api_host) {
      setTestStatus('error')
      setTestMessage('Please fill in the IP address')
      return
    }
    if (formData.api_type === 'bambu' && (!formData.serial || !formData.access_code)) {
      setTestStatus('error')
      setTestMessage('Please fill in Serial and Access Code')
      return
    }

    setTestStatus('testing')
    setTestMessage('Connecting to printer...')

    try {
      const result = await printers.testConnection({
        api_type: formData.api_type || 'bambu',
        plug_type: formData.plug_type || null,
        plug_host: formData.plug_host || null,
        plug_topic: formData.plug_topic || null,
        plug_entity: formData.plug_entity || null,
        plug_token: formData.plug_token || null,
        api_host: formData.api_host,
        serial: formData.serial,
        access_code: formData.access_code
      })

      if (result.success) {
        setTestStatus('success')
        setTestMessage(`Connected! State: ${result.state}, Bed: ${result.bed_temp}°C, ${result.ams_slots || 0} AMS slots`)
        if (result.ams_slots != null) {
          setFormData(prev => ({ ...prev, slot_count: result.ams_slots }))
        }
        if (result.model && result.model !== 'Unknown' && !formData.model) {
          setFormData(prev => ({ ...prev, model: result.model }))
        }
      } else {
        setTestStatus('error')
        setTestMessage(result.error || 'Connection failed')
      }
    } catch (err) {
      setTestStatus('error')
      setTestMessage('Failed to test connection')
    }
  }

  const handleSubmit = (e) => {
    e.preventDefault()

    const submitData = {
      name: formData.name,
      nickname: formData.nickname || null,
      model: formData.model,
      slot_count: formData.slot_count,
      camera_url: formData.camera_url || null,
      tags: formData.tags || [],
      timelapse_enabled: formData.timelapse_enabled || false,
      shared: formData.shared || false,
      bed_x_mm: formData.bed_x_mm !== '' ? parseFloat(formData.bed_x_mm) || null : null,
      bed_y_mm: formData.bed_y_mm !== '' ? parseFloat(formData.bed_y_mm) || null : null,
    }

    if (formData.api_type) submitData.api_type = formData.api_type
    if (formData.api_host) submitData.api_host = formData.api_host
    if (formData.serial && formData.access_code) {
      submitData.api_key = `${formData.serial}|${formData.access_code}`
    }

    onSubmit(submitData, printer?.id)
  }

  if (!isOpen) return null

  const isEditing = !!printer
  const showBambuFields = formData.api_type === 'bambu'
  const showConnectionFields = !!formData.api_type && formData.api_type !== ''

  return (
    <div className="fixed inset-0 bg-black/50 flex items-end sm:items-center justify-center z-50 p-0 sm:p-4" role="dialog" aria-modal="true" aria-labelledby="printer-modal-title">
      <div ref={modalRef} className="bg-farm-900 rounded-t-xl sm:rounded w-full max-w-md p-4 sm:p-6 border border-farm-700 max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-4">
          <h2 id="printer-modal-title" className="text-lg sm:text-xl font-display font-semibold">{isEditing ? 'Edit Printer' : 'Add New Printer'}</h2>
          <button onClick={onClose} className="text-farm-500 hover:text-farm-300" aria-label="Close printer form"><X size={20} /></button>
        </div>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm text-farm-400 mb-1">Machine ID {!isEditing && '*'}</label>
            <input type="text" required={!isEditing} value={formData.name} onChange={(e) => setFormData(prev => ({ ...prev, name: e.target.value.replace(/\s+/g, '-') }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm" placeholder="e.g., p1s-01, x1c-floor2" />
            <p className="text-xs text-farm-500 mt-1">Unique identifier used in logs and the API. Cannot contain spaces.</p>
          </div>
          <div>
            <label className="block text-sm text-farm-400 mb-1">Display Name</label>
            <input type="text" value={formData.nickname} onChange={(e) => setFormData(prev => ({ ...prev, nickname: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm" placeholder="e.g., Big Bertha (optional)" />
            <p className="text-xs text-farm-500 mt-1">Friendly label shown on cards and dashboards. Falls back to Machine ID if blank.</p>
          </div>
          {isEditing && showBambuFields && printer?.model && printer?.last_seen ? (
            <div>
              <label className="block text-sm text-farm-400 mb-1">Model</label>
              <div className="flex items-center gap-2">
                <span className="flex-1 bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm text-farm-200">{formData.model}</span>
                <span className="text-xs text-green-400 flex items-center gap-1 whitespace-nowrap"><Lock size={11} /> Auto-detected</span>
              </div>
              {!modelOverride ? (
                <button type="button" onClick={() => setModelOverride(true)} className="text-xs text-farm-500 hover:text-farm-300 mt-1">Override</button>
              ) : (
                <select value={formData.model} onChange={(e) => setFormData(prev => ({ ...prev, model: e.target.value }))} className="w-full bg-farm-800 border border-amber-600 rounded-lg px-3 py-2 text-sm mt-2">
                  <option value="">Select model</option>
                  {BAMBU_MODELS.map(m => <option key={m} value={m}>{m}</option>)}
                </select>
              )}
            </div>
          ) : showBambuFields ? (
            <div>
              <label className="block text-sm text-farm-400 mb-1">Model</label>
              <select value={formData.model} onChange={(e) => setFormData(prev => ({ ...prev, model: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm">
                <option value="">Will auto-detect on first connect</option>
                {BAMBU_MODELS.map(m => <option key={m} value={m}>{m}</option>)}
              </select>
            </div>
          ) : (
            <div>
              <label className="block text-sm text-farm-400 mb-1">Model</label>
              <input type="text" value={formData.model} onChange={(e) => setFormData(prev => ({ ...prev, model: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm" placeholder="e.g., Prusa MK4, Ender 3" />
            </div>
          )}
          {isEditing && showBambuFields && printer?.last_seen ? (
            <div>
              <label className="block text-sm text-farm-400 mb-1">Filament Slots</label>
              <div className="flex items-center gap-2">
                <span className="flex-1 bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm text-farm-200">{formData.slot_count} slots</span>
                <span className="text-xs text-green-400 flex items-center gap-1 whitespace-nowrap"><Lock size={11} /> Auto-detected</span>
              </div>
              {!slotOverride ? (
                <button type="button" onClick={() => setSlotOverride(true)} className="text-xs text-farm-500 hover:text-farm-300 mt-1">Override</button>
              ) : (
                <select value={formData.slot_count} onChange={(e) => setFormData(prev => ({ ...prev, slot_count: Number(e.target.value) }))} className="w-full bg-farm-800 border border-amber-600 rounded-lg px-3 py-2 text-sm mt-2">
                  <option value={1}>1 slot (no AMS)</option>
                  <option value={4}>4 slots (1x AMS)</option>
                  <option value={5}>5 slots (AMS + HT slot)</option>
                  <option value={8}>8 slots (2x AMS)</option>
                  <option value={9}>9 slots (2x AMS + HT slot)</option>
                  <option value={12}>12 slots (3x AMS)</option>
                  <option value={16}>16 slots (4x AMS)</option>
                </select>
              )}
              {slotOverride && formData.slot_count !== printer.slot_count && (
                <p className="text-xs text-amber-400 mt-1">Note: Changing slot count will reset filament colors</p>
              )}
            </div>
          ) : (
            <div>
              <label className="block text-sm text-farm-400 mb-1">Filament Slots</label>
              <select value={formData.slot_count} onChange={(e) => setFormData(prev => ({ ...prev, slot_count: Number(e.target.value) }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm">
                <option value={1}>1 slot (no AMS)</option>
                <option value={4}>4 slots (1x AMS)</option>
                <option value={5}>5 slots (AMS + HT slot)</option>
                <option value={8}>8 slots (2x AMS)</option>
                <option value={9}>9 slots (2x AMS + HT slot)</option>
                <option value={12}>12 slots (3x AMS)</option>
                <option value={16}>16 slots (4x AMS)</option>
              </select>
              {isEditing && formData.slot_count !== printer?.slot_count && (
                <p className="text-xs text-amber-400 mt-1">Note: Changing slot count will reset filament colors</p>
              )}
            </div>
          )}

          {isEditing && (printer?.nozzle_diameter || printer?.nozzle_type) && (
            <div className="flex gap-3">
              {printer.nozzle_diameter && (
                <div className="flex-1">
                  <label className="block text-xs text-farm-500 mb-1">Nozzle Size</label>
                  <div className="bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm text-farm-300">{printer.nozzle_diameter}mm</div>
                </div>
              )}
              {printer.nozzle_type && (
                <div className="flex-1">
                  <label className="block text-xs text-farm-500 mb-1">Nozzle Type</label>
                  <div className="bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm text-farm-300">{printer.nozzle_type}</div>
                </div>
              )}
            </div>
          )}

          <div>
            <label className="block text-sm text-farm-400 mb-1">Bed Size (optional)</label>
            <div className="flex gap-2 items-center">
              <input
                type="number"
                min="1"
                step="1"
                value={formData.bed_x_mm}
                onChange={(e) => setFormData(prev => ({ ...prev, bed_x_mm: e.target.value }))}
                className="flex-1 bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm"
                placeholder="X mm"
              />
              <span className="text-farm-500 text-sm">x</span>
              <input
                type="number"
                min="1"
                step="1"
                value={formData.bed_y_mm}
                onChange={(e) => setFormData(prev => ({ ...prev, bed_y_mm: e.target.value }))}
                className="flex-1 bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm"
                placeholder="Y mm"
              />
              <span className="text-farm-500 text-xs whitespace-nowrap">mm</span>
            </div>
            <p className="text-xs text-farm-600 mt-1">Used to verify file compatibility before dispatch</p>
          </div>

          <div className="border-t border-farm-700 pt-4 mt-4">
            <label className="block text-sm text-farm-400 mb-2">Printer Connection (Optional)</label>
            <select
              value={formData.api_type}
              onChange={(e) => setFormData(prev => ({ ...prev, api_type: e.target.value }))}
              className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm"
            >
              <option value="">Manual (no connection)</option>
              <option value="bambu">Bambu Lab (X1C, P1S, A1, etc.)</option>
              <option value="moonraker">Klipper / Moonraker</option>
              <option value="prusalink">Prusa (PrusaLink)</option>
              <option value="elegoo">Elegoo (SDCP)</option>
              <option value="octoprint" disabled>OctoPrint (coming soon)</option>
            </select>
          </div>

          {showConnectionFields && (
            <>
              <div>
                <label className="block text-sm text-farm-400 mb-1">Printer IP Address</label>
                <input type="text" value={formData.api_host} onChange={(e) => setFormData(prev => ({ ...prev, api_host: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm" placeholder="e.g., 192.168.1.100" />
                {formData.api_type === 'bambu' && <p className="text-xs text-farm-500 mt-1">Find this on the printer: Network settings</p>}
                {formData.api_type === 'moonraker' && <p className="text-xs text-farm-500 mt-1">IP of your Klipper printer running Moonraker</p>}
                {formData.api_type === 'prusalink' && <p className="text-xs text-farm-500 mt-1">IP of your Prusa printer with PrusaLink enabled</p>}
                {formData.api_type === 'elegoo' && <p className="text-xs text-farm-500 mt-1">IP of your Elegoo printer — SDCP on port 3030, no auth needed</p>}
              </div>
              {formData.api_type === 'bambu' && (
              <div>
                <label className="block text-sm text-farm-400 mb-1">Serial Number</label>
                <input type="text" value={formData.serial} onChange={(e) => setFormData(prev => ({ ...prev, serial: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 font-mono text-sm" placeholder="e.g., 00M09A380700000" />
                <p className="text-xs text-farm-500 mt-1">Find this on the printer: Settings → Device Info</p>
              </div>
              )}
              {formData.api_type === 'bambu' && (
              <div>
                <label className="block text-sm text-farm-400 mb-1">Access Code</label>
                <input type="text" value={formData.access_code} onChange={(e) => setFormData(prev => ({ ...prev, access_code: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 font-mono text-sm" placeholder="e.g., 12345678" />
                <p className="text-xs text-farm-500 mt-1">Find this on the printer: Settings → Network → Access Code</p>
              </div>
              )}
              {(formData.api_type === 'moonraker' || formData.api_type === 'prusalink') && (
              <div>
                <label className="block text-sm text-farm-400 mb-1">API Key {formData.api_type === 'prusalink' ? '' : '(optional)'}</label>
                <input type="text" value={formData.api_key_field || ''} onChange={(e) => setFormData(prev => ({ ...prev, api_key_field: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 font-mono text-sm" placeholder={formData.api_type === 'prusalink' ? 'PrusaLink API key from Settings' : 'Moonraker API key (if required)'} />
                {formData.api_type === 'prusalink' && <p className="text-xs text-farm-500 mt-1">Find in PrusaLink → Settings → API Key</p>}
              </div>
              )}

              <div>
                <button
                  type="button"
                  onClick={handleTestConnection}
                  disabled={testStatus === 'testing'}
                  className={clsx(
                    "w-full px-4 py-2 rounded-lg transition-colors flex items-center justify-center gap-2 text-sm",
                    testStatus === 'testing' && "bg-farm-700 text-farm-400 cursor-wait",
                    testStatus === 'success' && "bg-green-900/50 text-green-400 border border-green-700",
                    testStatus === 'error' && "bg-red-900/50 text-red-400 border border-red-700",
                    !testStatus && "bg-farm-700 hover:bg-farm-600 text-farm-200"
                  )}
                >
                  {testStatus === 'testing' ? (
                    <><span className="animate-spin">⟳</span> Testing...</>
                  ) : testStatus === 'success' ? (
                    '✓ Connected!'
                  ) : testStatus === 'error' ? (
                    '✗ Failed'
                  ) : (
                    'Test Connection'
                  )}
                </button>
                {testMessage && (
                  <p className={clsx("text-xs mt-1", testStatus === 'success' ? "text-green-400" : testStatus === 'error' ? "text-red-400" : "text-farm-400")}>
                    {testMessage}
                  </p>
                )}
              </div>
            </>
          )}

          {/* Smart Plug Configuration */}
          {formData.api_type && (
          <div className="border-t border-farm-700 pt-4 mt-4">
            <label className="block text-sm text-farm-400 mb-2">Smart Plug (Optional)</label>
            <select
              value={formData.plug_type || ''}
              onChange={(e) => setFormData(prev => ({ ...prev, plug_type: e.target.value }))}
              className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm mb-3"
            >
              <option value="">No smart plug</option>
              <option value="tasmota">Tasmota (HTTP)</option>
              <option value="mqtt">MQTT Plug</option>
              <option value="homeassistant">Home Assistant</option>
            </select>
            {formData.plug_type === 'tasmota' && (
              <div className="space-y-2">
                <input type="text" value={formData.plug_host || ''} onChange={(e) => setFormData(prev => ({ ...prev, plug_host: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm" placeholder="Tasmota IP (e.g., 192.168.1.50)" />
                <p className="text-xs text-farm-500">Auto power-on before print, auto power-off after cooldown</p>
              </div>
            )}
            {formData.plug_type === 'mqtt' && (
              <div className="space-y-2">
                <input type="text" value={formData.plug_topic || ''} onChange={(e) => setFormData(prev => ({ ...prev, plug_topic: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm" placeholder="MQTT topic (e.g., cmnd/plug1/POWER)" />
                <p className="text-xs text-farm-500">Publishes ON/OFF to the configured MQTT broker</p>
              </div>
            )}
            {formData.plug_type === 'homeassistant' && (
              <div className="space-y-2">
                <input type="text" value={formData.plug_host || ''} onChange={(e) => setFormData(prev => ({ ...prev, plug_host: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm" placeholder="HA URL (e.g., http://homeassistant.local:8123)" />
                <input type="text" value={formData.plug_entity || ''} onChange={(e) => setFormData(prev => ({ ...prev, plug_entity: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm" placeholder="Entity ID (e.g., switch.printer_plug)" />
                <input type="password" value={formData.plug_token || ''} onChange={(e) => setFormData(prev => ({ ...prev, plug_token: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm" placeholder="Long-lived access token" />
                <p className="text-xs text-farm-500">Uses HA REST API to control the switch entity</p>
              </div>
            )}
          </div>
          )}

          {/* Camera URL */}
          <div className="border-t border-farm-700 pt-4 mt-4">
            <label className="block text-sm text-farm-400 mb-1">Camera URL (optional)</label>
            <input type="text" value={formData.camera_url} onChange={(e) => setFormData(prev => ({ ...prev, camera_url: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm" placeholder="e.g. http://192.168.1.50:8080/?action=stream" />
            {formData.api_type === 'bambu' && /^(P1S|P1P|A1)/i.test(formData.model) ? (
              <p className="text-xs text-farm-500 mt-1">P1S/A1 built-in cameras require LAN Live View which isn't available on these models. Use an external camera (USB webcam + MJPEG streamer, or IP camera) for Vigil AI.</p>
            ) : formData.api_type === 'bambu' && /^(X1|H2D)/i.test(formData.model) ? (
              <p className="text-xs text-farm-500 mt-1">Leave blank to auto-detect built-in camera.</p>
            ) : (
              <p className="text-xs text-farm-500 mt-1">MJPEG, RTSP, or HTTP snapshot URL for Vigil AI monitoring.</p>
            )}
          </div>

          {/* Tags */}
          <div className="border-t border-farm-700 pt-4 mt-4">
            <label className="block text-sm text-farm-400 mb-1">Tags (optional)</label>
            <div className="flex flex-wrap gap-1.5 mb-2">
              {(formData.tags || []).map(tag => (
                <span key={tag} className="flex items-center gap-1 px-2 py-0.5 bg-print-600/20 text-print-400 text-xs rounded-full border border-print-600/30">
                  {tag}
                  <button type="button" onClick={() => setFormData(prev => ({ ...prev, tags: prev.tags.filter(t => t !== tag) }))} className="hover:text-red-400">&times;</button>
                </span>
              ))}
            </div>
            <input
              type="text"
              placeholder="Type a tag and press Enter"
              className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm"
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault()
                  const val = e.target.value.trim()
                  if (val && !(formData.tags || []).includes(val)) {
                    setFormData(prev => ({ ...prev, tags: [...(prev.tags || []), val] }))
                  }
                  e.target.value = ''
                }
              }}
            />
            <p className="text-xs text-farm-500 mt-1">e.g., "Room A", "PLA-only", "Production"</p>
          </div>

          {/* Timelapse */}
          <div className="border-t border-farm-700 pt-4 mt-4">
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={formData.timelapse_enabled || false}
                onChange={(e) => setFormData(prev => ({ ...prev, timelapse_enabled: e.target.checked }))}
                className="w-4 h-4 rounded border-farm-600 bg-farm-800 text-print-600 focus:ring-print-600"
              />
              <span className="text-sm text-farm-200">Enable timelapse recording</span>
            </label>
            <p className="text-xs text-farm-500 mt-1 ml-6">Capture frames every 30s during prints and stitch into MP4 videos.</p>
          </div>

          {/* Shared across orgs (admin only) */}
          {canDo('settings.edit') && (
            <div className="border-t border-farm-700 pt-4 mt-4">
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={formData.shared || false}
                  onChange={(e) => setFormData(prev => ({ ...prev, shared: e.target.checked }))}
                  className="w-4 h-4 rounded border-farm-600 bg-farm-800 text-print-600 focus:ring-print-600"
                />
                <span className="text-sm text-farm-200">Shared across organizations</span>
              </label>
              <p className="text-xs text-farm-500 mt-1 ml-6">Visible to all organizations regardless of assignment.</p>
            </div>
          )}

          <div className="flex justify-end gap-3 pt-4">
            <button type="button" onClick={onClose} className="px-4 py-2 bg-farm-800 hover:bg-farm-700 rounded-lg transition-colors text-sm">Cancel</button>
            <button type="submit" className="px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg transition-colors text-sm">{isEditing ? 'Save Changes' : 'Add Printer'}</button>
          </div>
        </form>
      </div>
    </div>
  )
}
