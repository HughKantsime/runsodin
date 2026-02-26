import { useState, useEffect, useRef } from 'react'
import { Save, Eye, Upload, CheckCircle } from 'lucide-react'
import { vision, printers } from '../../api'

function ModelUpload({ onUploaded }) {
  const [uploading, setUploading] = useState(false)
  const fileRef = useRef(null)

  const handleUpload = async () => {
    const file = fileRef.current?.files?.[0]
    if (!file) return
    setUploading(true)
    try {
      const name = file.name.replace('.onnx', '')
      const dt = name.includes('spaghetti') ? 'spaghetti' : name.includes('first') ? 'first_layer' : name.includes('detach') ? 'detachment' : 'spaghetti'
      const data = await vision.uploadModel(file, name, dt)
      onUploaded(data)
      fileRef.current.value = ''
    } catch (e) { console.error('Upload failed:', e) }
    setUploading(false)
  }

  return (
    <div className="flex items-center gap-2">
      <input ref={fileRef} type="file" accept=".onnx" className="text-xs text-farm-400" />
      <button
        onClick={handleUpload}
        disabled={uploading}
        className="flex items-center gap-1.5 px-3 py-1.5 bg-farm-700 hover:bg-farm-600 rounded-lg text-xs font-medium transition-colors disabled:opacity-50"
      >
        <Upload size={12} />
        {uploading ? 'Uploading...' : 'Upload Model'}
      </button>
    </div>
  )
}

export default function VisionSettingsTab() {
  const [globalSettings, setGlobalSettings] = useState({ enabled: true, retention_days: 30 })
  const [printerSettings, setPrinterSettings] = useState([])
  const [models, setModels] = useState([])
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState('')

  useEffect(() => {
    // Load global settings
    vision.getSettings().then(setGlobalSettings).catch(() => {})
    // Load models
    vision.getModels().then(setModels).catch(() => {})
    // Load printers with vision settings
    printers.list().then(async (printerList) => {
      const withVision = await Promise.all(
        printerList.filter(p => p.camera_url).map(async (p) => {
          try {
            const vs = await vision.getPrinterSettings(p.id)
            return { ...p, vision: vs }
          } catch { return { ...p, vision: null } }
        })
      )
      setPrinterSettings(withVision)
    }).catch(() => {})
  }, [])

  const saveGlobal = async () => {
    setSaving(true)
    try {
      await vision.updateSettings(globalSettings)
      setMsg('Settings saved')
      setTimeout(() => setMsg(''), 2000)
    } catch { setMsg('Save failed') }
    setSaving(false)
  }

  const savePrinterVision = async (printerId, data) => {
    try {
      await vision.updatePrinterSettings(printerId, data)
      setPrinterSettings(prev => prev.map(p =>
        p.id === printerId ? { ...p, vision: { ...p.vision, ...data } } : p
      ))
    } catch (e) { console.error('Failed to save printer vision settings:', e) }
  }

  const activateModel = async (modelId) => {
    try {
      const data = await vision.activateModel(modelId)
      setModels(prev => prev.map(m => ({
        ...m,
        is_active: m.detection_type === data.detection_type ? (m.id === modelId ? 1 : 0) : m.is_active
      })))
    } catch (e) { console.error('Failed to activate model:', e) }
  }

  return (
    <div className="max-w-4xl space-y-6">
      {/* Global Settings */}
      <div className="bg-farm-900 rounded-lg border border-farm-800 p-4 md:p-6">
        <div className="flex items-center gap-2 mb-4">
          <Eye size={18} className="text-purple-400" />
          <h2 className="text-lg font-display font-semibold">Vigil AI</h2>
        </div>
        <p className="text-sm text-farm-400 mb-4">
          AI-powered print failure detection using local ONNX inference. No cloud services — all processing runs on this server.
        </p>

        <div className="space-y-4">
          <label className="flex items-center gap-3">
            <input
              type="checkbox"
              checked={globalSettings.enabled}
              onChange={e => setGlobalSettings(s => ({ ...s, enabled: e.target.checked }))}
              className="rounded-lg"
            />
            <span className="text-sm">Enable Vigil AI globally</span>
          </label>

          <div className="flex items-center gap-3">
            <label className="text-sm text-farm-400">Frame retention:</label>
            <input
              type="range" min="7" max="90" step="1"
              value={globalSettings.retention_days}
              onChange={e => setGlobalSettings(s => ({ ...s, retention_days: parseInt(e.target.value) }))}
              className="flex-1 max-w-xs"
            />
            <span className="text-sm w-16">{globalSettings.retention_days} days</span>
          </div>

          <div className="flex items-center gap-2">
            <button onClick={saveGlobal} disabled={saving} className="px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg text-sm font-medium transition-colors disabled:opacity-50">
              <Save size={14} className="inline mr-1.5" />
              Save
            </button>
            {msg && <span className="text-sm text-green-400">{msg}</span>}
          </div>
        </div>
      </div>

      {/* Per-Printer Settings */}
      <div className="bg-farm-900 rounded-lg border border-farm-800 p-4 md:p-6">
        <h3 className="text-base font-semibold mb-3">Per-Printer Detection Settings</h3>
        {printerSettings.length === 0 && (
          <p className="text-sm text-farm-500">No printers with cameras configured</p>
        )}
        <div className="space-y-3">
          {printerSettings.map(p => {
            const vs = p.vision || {}
            return (
              <div key={p.id} className="bg-farm-800 rounded-lg p-3">
                <div className="flex items-center justify-between mb-2">
                  <span className="font-medium text-sm">{p.nickname || p.name}</span>
                  <label className="flex items-center gap-2 text-xs">
                    <input
                      type="checkbox"
                      checked={vs.enabled !== 0}
                      onChange={e => savePrinterVision(p.id, { enabled: e.target.checked ? 1 : 0 })}
                    />
                    Enabled
                  </label>
                </div>
                {vs.enabled !== 0 && (
                  <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
                    {/* Spaghetti */}
                    <div>
                      <label className="flex items-center gap-1.5 mb-1">
                        <input type="checkbox" checked={vs.spaghetti_enabled !== 0}
                          onChange={e => savePrinterVision(p.id, { spaghetti_enabled: e.target.checked ? 1 : 0 })}
                        />
                        <span className="text-red-400">Spaghetti</span>
                      </label>
                      <input type="range" min="0.3" max="0.95" step="0.05"
                        value={vs.spaghetti_threshold || 0.65}
                        onChange={e => savePrinterVision(p.id, { spaghetti_threshold: parseFloat(e.target.value) })}
                        className="w-full"
                      />
                      <span className="text-farm-500">{((vs.spaghetti_threshold || 0.65) * 100).toFixed(0)}%</span>
                    </div>

                    {/* First Layer */}
                    <div>
                      <label className="flex items-center gap-1.5 mb-1">
                        <input type="checkbox" checked={vs.first_layer_enabled !== 0}
                          onChange={e => savePrinterVision(p.id, { first_layer_enabled: e.target.checked ? 1 : 0 })}
                        />
                        <span className="text-amber-400">First Layer</span>
                      </label>
                      <input type="range" min="0.3" max="0.95" step="0.05"
                        value={vs.first_layer_threshold || 0.60}
                        onChange={e => savePrinterVision(p.id, { first_layer_threshold: parseFloat(e.target.value) })}
                        className="w-full"
                      />
                      <span className="text-farm-500">{((vs.first_layer_threshold || 0.60) * 100).toFixed(0)}%</span>
                    </div>

                    {/* Detachment */}
                    <div>
                      <label className="flex items-center gap-1.5 mb-1">
                        <input type="checkbox" checked={vs.detachment_enabled !== 0}
                          onChange={e => savePrinterVision(p.id, { detachment_enabled: e.target.checked ? 1 : 0 })}
                        />
                        <span className="text-orange-400">Detachment</span>
                      </label>
                      <input type="range" min="0.3" max="0.95" step="0.05"
                        value={vs.detachment_threshold || 0.70}
                        onChange={e => savePrinterVision(p.id, { detachment_threshold: parseFloat(e.target.value) })}
                        className="w-full"
                      />
                      <span className="text-farm-500">{((vs.detachment_threshold || 0.70) * 100).toFixed(0)}%</span>
                    </div>

                    {/* Options */}
                    <div className="space-y-1.5">
                      <label className="flex items-center gap-1.5">
                        <input type="checkbox" checked={vs.auto_pause === 1}
                          onChange={e => savePrinterVision(p.id, { auto_pause: e.target.checked ? 1 : 0 })}
                        />
                        Auto-pause
                      </label>
                      <label className="flex items-center gap-1.5">
                        <input type="checkbox" checked={vs.collect_training_data === 1}
                          onChange={e => savePrinterVision(p.id, { collect_training_data: e.target.checked ? 1 : 0 })}
                        />
                        Collect data
                      </label>
                      <div className="flex items-center gap-1">
                        <span className="text-farm-500">Interval:</span>
                        <select
                          value={vs.capture_interval_sec || 10}
                          onChange={e => savePrinterVision(p.id, { capture_interval_sec: parseInt(e.target.value) })}
                          className="bg-farm-700 border border-farm-600 rounded-lg px-1 py-0.5 text-xs"
                        >
                          <option value={5}>5s</option>
                          <option value={10}>10s</option>
                          <option value={15}>15s</option>
                          <option value={30}>30s</option>
                          <option value={60}>60s</option>
                        </select>
                      </div>
                    </div>
                  </div>
                )}
              </div>
            )
          })}
        </div>
      </div>

      {/* Model Management */}
      <div className="bg-farm-900 rounded-lg border border-farm-800 p-4 md:p-6">
        <h3 className="text-base font-semibold mb-3">ONNX Models</h3>
        {models.length === 0 && (
          <p className="text-sm text-farm-500">No models uploaded. Upload ONNX models to enable detection.</p>
        )}
        <div className="space-y-2">
          {models.map(m => (
            <div key={m.id} className="flex items-center justify-between py-2 px-3 bg-farm-800 rounded-lg">
              <div className="flex items-center gap-3">
                <span className="text-sm font-medium">{m.name}</span>
                <span className="text-xs text-farm-500">{m.detection_type}</span>
                {m.version && <span className="text-xs text-farm-600">v{m.version}</span>}
              </div>
              <div className="flex items-center gap-2">
                {m.is_active ? (
                  <span className="flex items-center gap-1 text-xs text-green-400">
                    <CheckCircle size={12} />
                    Active
                  </span>
                ) : (
                  <button
                    onClick={() => activateModel(m.id)}
                    className="text-xs text-farm-400 hover:text-white px-2 py-1 bg-farm-700 rounded-lg transition-colors"
                  >
                    Activate
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>

        {/* Upload */}
        <div className="mt-3">
          <ModelUpload onUploaded={(m) => setModels(prev => [m, ...prev])} />
        </div>
      </div>
    </div>
  )
}
