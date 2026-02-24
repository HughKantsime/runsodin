import QRScannerModal from '../components/QRScannerModal';
import { useState, useEffect, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2, Power, PowerOff, Palette, X, Settings, Search, GripVertical, RefreshCw, AlertTriangle, Lightbulb, Activity, CircleDot, Filter, ArrowUpDown, Video, QrCode, Thermometer, Plug, Printer as PrinterIcon, Lock, ExternalLink } from 'lucide-react'
import clsx from 'clsx'
import toast from 'react-hot-toast'
import AmsEnvironmentChart from '../components/AmsEnvironmentChart'
import PrinterTelemetryChart from '../components/PrinterTelemetryChart'
import NozzleStatusCard from '../components/NozzleStatusCard'
import HmsHistoryPanel from '../components/HmsHistoryPanel'
import ConfirmModal from '../components/ConfirmModal'
import { getPlugState, plugPowerOn, plugPowerOff } from '../api'
import { printers, filaments, bulkOps, spools as spoolsApi, fetchAPI } from '../api'
import { canDo } from '../permissions'
import { useLicense } from '../LicenseContext'
import UpgradeModal from '../components/UpgradeModal'
import { useOrg } from '../contexts/OrgContext'
import CameraModal from '../components/CameraModal'
import { getShortName } from '../utils/shared'

function FilamentSlotEditor({ slot, allFilaments, spools, printerId, onSave }) {
  const [isEditing, setIsEditing] = useState(false)
  const [search, setSearch] = useState('')
  const filteredFilaments = allFilaments?.filter(f => 
    f.display_name.toLowerCase().includes(search.toLowerCase()) ||
    f.brand.toLowerCase().includes(search.toLowerCase()) ||
    f.name.toLowerCase().includes(search.toLowerCase())
  ) || []

  const spoolmanFilaments = filteredFilaments.filter(f => f.source === 'spoolman')
  const libraryFilaments = filteredFilaments.filter(f => f.source === 'library')
  
  const filteredSpools = (spools?.filter(s => 
    s.filament_brand?.toLowerCase().includes(search.toLowerCase()) ||
    s.filament_name?.toLowerCase().includes(search.toLowerCase())
  ).sort((a, b) => {
    if (a.id === slot.assigned_spool_id) return -1;
    if (b.id === slot.assigned_spool_id) return 1;
    return 0;
  })) || []
  
  const handleSelectSpool = async (spool) => {
    await printers.assignSlotSpool(printerId, slot.slot_number, spool.id)
    onSave({
      color: `${spool.filament_brand} ${spool.filament_name}`,
      color_hex: spool.filament_color_hex,
      filament_type: spool.filament_material,
      assigned_spool_id: spool.id
    })
    setIsEditing(false)
    setSearch("")
  }

  const handleSelect = (filament) => {
    onSave({ 
      color: `${filament.brand} ${filament.name}`,
      color_hex: filament.color_hex,
      filament_type: filament.material,
      spoolman_spool_id: filament.source === 'spoolman' ? parseInt(filament.id.replace('spool_', '')) : null
    })
    setIsEditing(false)
    setSearch('')
  }

  const handleClear = () => {
    onSave({ filament_type: 'empty', color: null, color_hex: null, spoolman_spool_id: null })
    setIsEditing(false)
  }

  const colorHex = slot.color_hex

  return (
    <>
      <div 
        className="bg-farm-800 rounded-lg p-2 cursor-pointer hover:bg-farm-700 transition-colors min-w-0 text-center" 
        onClick={() => { if (typeof canDo === 'function' ? canDo('printers.slots') : true) setIsEditing(true) }}
      >
        <div className="w-full h-3 rounded-lg mb-1" style={{ backgroundColor: colorHex ? `#${colorHex}` : "#333" }}/>
        <span className="text-xs text-farm-500 truncate block">{getShortName(slot)}</span>
      </div>
      
      {isEditing && (
        <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center" role="dialog" aria-modal="true" aria-label={`Select filament for slot ${slot.slot_number}`}>
          <div
            className="absolute inset-0 bg-black/50"
            onClick={() => { setIsEditing(false); setSearch('') }}
          />
          <div className="relative bg-farm-800 rounded-t-xl sm:rounded p-4 w-full sm:w-80 shadow-xl border border-farm-600 max-h-[80vh] flex flex-col">
            <div className="text-sm font-medium text-farm-300 mb-3">Slot {slot.slot_number} - Select Filament</div>
            <div className="relative mb-3">
              <Search size={14} className="absolute left-2 top-1/2 -translate-y-1/2 text-farm-500" />
              <input
                type="text"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search filaments..."
                className="w-full bg-farm-900 border border-farm-600 rounded-lg pl-8 pr-3 py-2 text-sm"
                autoFocus
              />
            </div>
            <div className="flex-1 overflow-y-auto space-y-1 max-h-96">
              {filteredSpools.length > 0 && (
                <>
                  <div className="text-xs text-green-400 font-medium px-1 py-1">Tracked Spools</div>
                  {filteredSpools.map(s => (
                    <button
                      key={s.id}
                      onClick={() => handleSelectSpool(s)}
                      className="w-full flex items-center gap-2 px-2 py-2 hover:bg-farm-700 rounded-lg text-left text-sm"
                    >
                      <div 
                        className="w-5 h-5 rounded-lg border border-farm-500 flex-shrink-0" 
                        style={{ backgroundColor: s.filament_color_hex ? `#${s.filament_color_hex}` : "#666" }}
                      />
                      <span className="truncate flex-1">{s.filament_brand} {s.filament_name}</span>
                      <span className="text-xs text-farm-400">{Math.round(s.remaining_weight_g)}g</span>
                    </button>
                  ))}
                </>
              )}
              {spoolmanFilaments.length > 0 && (
                <>
                  <div className="text-xs text-print-400 font-medium px-1 py-1">From Spoolman</div>
                  {spoolmanFilaments.slice(0, 15).map(f => (
                    <button
                      key={f.id}
                      onClick={() => handleSelect(f)}
                      className="w-full flex items-center gap-2 px-2 py-2 hover:bg-farm-700 rounded-lg text-left text-sm"
                    >
                      <div 
                        className="w-5 h-5 rounded-lg border border-farm-500 flex-shrink-0" 
                        style={{ backgroundColor: f.color_hex ? `#${f.color_hex}` : '#666' }}
                      />
                      <span className="truncate flex-1">{f.name}</span>
                      {f.remaining_weight && (
                        <span className="text-xs text-farm-400">{Math.round(f.remaining_weight)}g</span>
                      )}
                    </button>
                  ))}
                </>
              )}
              {libraryFilaments.length > 0 && (
                <>
                  <div className="text-xs text-farm-400 font-medium px-1 py-1 mt-2">From Library</div>
                  {libraryFilaments.slice(0, 20).map(f => (
                    <button
                      key={f.id}
                      onClick={() => handleSelect(f)}
                      className="w-full flex items-center gap-2 px-2 py-2 hover:bg-farm-700 rounded-lg text-left text-sm"
                    >
                      <div 
                        className="w-5 h-5 rounded-lg border border-farm-500 flex-shrink-0" 
                        style={{ backgroundColor: f.color_hex ? `#${f.color_hex}` : '#666' }}
                      />
                      <span className="truncate">{f.display_name}</span>
                    </button>
                  ))}
                </>
              )}
              {filteredFilaments.length === 0 && (
                <div className="text-sm text-farm-500 px-1 py-4 text-center">No filaments found</div>
              )}
            </div>
            <div className="flex gap-2 mt-4 flex-shrink-0">
              <button onClick={handleClear} className="flex-1 text-sm bg-farm-700 hover:bg-farm-600 rounded-lg py-2">Clear</button>
              <button onClick={() => { setIsEditing(false); setSearch('') }} className="flex-1 text-sm bg-farm-700 hover:bg-farm-600 rounded-lg py-2">Cancel</button>
            </div>
          </div>
        </div>
      )}
    </>
  )
}

function PrinterCard({ printer, allFilaments, spools, onDelete, onToggleActive, onUpdateSlot, onEdit, onSyncAms, isDragging, onDragStart, onDragOver, onDragEnd, hasCamera, onCameraClick, onScanSpool, onPlugToggle, plugStates }) {
  const [syncing, setSyncing] = useState(false)
  const [activePanel, setActivePanel] = useState(null)
  
  const handleSyncAms = async () => {
    setSyncing(true)
    try {
      await onSyncAms(printer.id)
    } finally {
      setSyncing(false)
    }
  }
  
  const hasBambuConnection = printer.api_type === 'bambu' && printer.api_host && printer.api_key
  
  const slotsNeedingAttention = printer.filament_slots?.filter(s => 
    (s.assigned_spool_id && !s.spool_confirmed) || (!s.assigned_spool_id && s.color_hex)
  ).length || 0
  
  return (
    <div 
      className={clsx(
        "bg-farm-900 rounded-lg border overflow-hidden h-fit transition-all",
        isDragging ? "border-print-500 opacity-50 scale-95" : "border-farm-800"
      )}
      draggable={!!onDragStart}
      onDragStart={onDragStart}
      onDragOver={onDragOver}
      onDragEnd={onDragEnd}
    >
      <div className="p-3 md:p-4 border-b border-farm-800 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 md:gap-3 min-w-0">
          <div className="cursor-grab active:cursor-grabbing text-farm-600 hover:text-farm-400 flex-shrink-0">
            <GripVertical size={16} />
          </div>
          <div className="min-w-0">
            <h3 className="font-display font-semibold text-base md:text-lg truncate">{printer.nickname || printer.name}</h3>
            <div className="flex items-center gap-1.5 flex-wrap">
              <span className="text-xs md:text-sm text-farm-500 truncate">{printer.model || 'Unknown model'}</span>
              {printer.tags?.map(tag => (
                <span key={tag} className="px-1.5 py-0.5 bg-print-600/20 text-print-400 text-[10px] rounded-full border border-print-600/30">{tag}</span>
              ))}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-1 flex-shrink-0">
          {canDo('printers.edit') && <button onClick={() => onEdit(printer)} className="p-1.5 md:p-2 text-farm-400 hover:bg-farm-800 rounded-lg transition-colors" aria-label="Edit printer settings">
            <Settings size={16} />
          </button>}
          {canDo('printers.edit') && <button onClick={() => onToggleActive(printer.id, !printer.is_active)} className={clsx('p-1.5 md:p-2 rounded-lg transition-colors', printer.is_active ? 'text-print-400 hover:bg-print-900/50' : 'text-farm-500 hover:bg-farm-800')} aria-label={printer.is_active ? 'Deactivate printer' : 'Activate printer'}>
            {printer.is_active ? <Power size={16} /> : <PowerOff size={16} />}
          </button>}
          {hasCamera && <button onClick={() => onCameraClick(printer)} className="p-1.5 md:p-2 text-farm-400 hover:bg-farm-800 rounded-lg transition-colors" aria-label="View camera">
            <Video size={16} />
          </button>}
          <button onClick={() => { navigator.clipboard.writeText(`${window.location.origin}/overlay/${printer.id}`); toast.success('Overlay URL copied') }} className="p-1.5 md:p-2 text-farm-400 hover:bg-farm-800 rounded-lg transition-colors" aria-label="Copy OBS overlay URL" title="Copy OBS overlay URL">
            <ExternalLink size={16} />
          </button>
          {printer.plug_type && onPlugToggle && (
            <button
              onClick={() => onPlugToggle(printer.id)}
              className={`p-1.5 md:p-2 rounded-lg transition-colors ${plugStates?.[printer.id] ? 'text-green-400 hover:bg-green-900/30' : 'text-farm-500 hover:bg-farm-800'}`}
              aria-label={plugStates?.[printer.id] ? 'Power off plug' : 'Power on plug'}
            >
              <Plug size={16} />
            </button>
          )}
          {onScanSpool && <button onClick={onScanSpool} className="p-1.5 md:p-2 text-farm-400 hover:bg-farm-800 rounded-lg transition-colors" aria-label="Scan spool QR code">
            <QrCode size={16} />
          </button>}
          {canDo('printers.delete') && <button onClick={() => onDelete(printer.id)} className="p-1.5 md:p-2 text-farm-500 hover:text-red-400 hover:bg-red-900/50 rounded-lg transition-colors" aria-label="Delete printer">
            <Trash2 size={16} />
          </button>}
        </div>
      </div>
      <div className="p-3 md:p-4">
        <div className="flex items-center gap-2 mb-3 flex-wrap">
          <Palette size={14} className="text-farm-500" />
          <span className="text-xs md:text-sm text-farm-400">Loaded Filaments</span>
          {slotsNeedingAttention > 0 && (
            <span className="flex items-center gap-1 text-xs text-yellow-400" title="Slots need spool assignment">
              <AlertTriangle size={12} />
              {slotsNeedingAttention}
            </span>
          )}
          {canDo('printers.slots') && hasBambuConnection ? (
            <button 
              onClick={handleSyncAms}
              disabled={syncing}
              className="ml-auto text-xs px-2 py-1 bg-farm-700 hover:bg-farm-600 rounded-lg transition-colors disabled:opacity-50"
              title="Sync filament state from printer"
            >
              {syncing ? '⟳ Syncing...' : '↻ Sync AMS'}
            </button>
          ) : (
            <span className="text-xs text-farm-600 ml-auto">(click to edit)</span>
          )}
        </div>
        <div className={clsx(
          "grid gap-2",
          printer.filament_slots?.length <= 4 ? "grid-cols-4" : "grid-cols-4"
        )}>
          {printer.filament_slots?.map((slot) => (
            <FilamentSlotEditor 
              printerId={printer.id}
              spools={spools}
              key={slot.id} 
              slot={slot} 
              allFilaments={allFilaments}
              onSave={(data) => onUpdateSlot(printer.id, slot.slot_number, data)} 
            />
          ))}
        </div>
      </div>
      <div className="px-3 md:px-4 py-2 md:py-3 bg-farm-950 border-t border-farm-800">
        {(() => {
          const online = printer.last_seen && (Date.now() - new Date(printer.last_seen + 'Z').getTime()) < 90000
          const bedTemp = printer.bed_temp != null ? Math.round(printer.bed_temp) : null
          const nozTemp = printer.nozzle_temp != null ? Math.round(printer.nozzle_temp) : null
          const bedTarget = printer.bed_target_temp != null ? Math.round(printer.bed_target_temp) : null
          const nozTarget = printer.nozzle_target_temp != null ? Math.round(printer.nozzle_target_temp) : null
          const isHeating = (bedTarget && bedTarget > 0) || (nozTarget && nozTarget > 0)
          const stage = printer.print_stage && printer.print_stage !== 'Idle' ? printer.print_stage : null
          return (
            <div className="flex items-center justify-between text-xs">
              <div className="flex items-center gap-2">
                <div className="flex items-center gap-1.5">
                  <div className={`w-2 h-2 rounded-full ${online ? "bg-green-500" : "bg-farm-600"}`}></div>
                  <span className={online ? "text-green-400" : "text-farm-500"}>{online ? "Online" : "Offline"}</span>
                </div>
                {printer.lights_on != null && (
                  <button
                    onClick={(e) => { e.stopPropagation(); printers.toggleLights(printer.id) }}
                    className={`p-0.5 rounded-lg transition-colors ${printer.lights_on ? 'text-yellow-400 hover:text-yellow-300' : 'text-farm-600 hover:text-farm-400'}`}
                    aria-label={printer.lights_on ? 'Turn lights off' : 'Turn lights on'}
                  >
                    <Lightbulb size={14} />
                  </button>
                )}
              </div>
              <div className="flex items-center gap-3 text-farm-400">
                {nozTemp != null && (
                  <span className={isHeating ? "text-orange-400" : ""} title={nozTarget > 0 ? `Nozzle: ${nozTemp}°/${nozTarget}°C` : `Nozzle: ${nozTemp}°C`}>
                    Nozzle {nozTemp}°{nozTarget > 0 ? `/${nozTarget}°` : ''}
                  </span>
                )}
                {bedTemp != null && (
                  <span className={bedTarget > 0 ? "text-orange-400" : ""} title={bedTarget > 0 ? `Bed: ${bedTemp}°/${bedTarget}°C` : `Bed: ${bedTemp}°C`}>
                    Bed {bedTemp}°{bedTarget > 0 ? `/${bedTarget}°` : ''}
                  </span>
                )}
                {stage && (
                  <span className="text-print-400">{stage}</span>
                )}
              </div>
            </div>
          )
        })()}
      </div>
      {/* Bambu speed control — active prints only */}
      {printer.api_type === 'bambu' && printer.gcode_state && ['RUNNING', 'PAUSE'].includes(printer.gcode_state.toUpperCase()) && (
        <div className="px-3 md:px-4 py-2 border-t border-farm-800">
          <div className="flex items-center gap-1">
            <span className="text-xs text-farm-500 mr-1">Speed</span>
            {[
              { level: 1, label: 'Silent', icon: '🐢' },
              { level: 2, label: 'Standard', icon: '▶' },
              { level: 3, label: 'Sport', icon: '⚡' },
              { level: 4, label: 'Ludicrous', icon: '🚀' },
            ].map(s => (
              <button
                key={s.level}
                onClick={(e) => { e.stopPropagation(); printers.setSpeed(printer.id, s.level) }}
                className="px-2 py-1 rounded text-xs transition-colors bg-farm-800 text-farm-400 hover:bg-farm-700 hover:text-farm-200"
                title={s.label}
              >
                {s.icon}
              </button>
            ))}
          </div>
        </div>
      )}
      {/* Data & Diagnostics toolbar */}
      <div className="px-3 md:px-4 py-2 border-t border-farm-800 flex items-center gap-1">
        <span className="text-xs text-farm-600 mr-1">Data</span>
        <button onClick={() => setActivePanel(activePanel === 'ams' ? null : 'ams')}
          className={clsx('p-1.5 rounded-lg text-xs flex items-center gap-1 transition-colors',
            activePanel === 'ams' ? 'bg-print-600/20 text-print-400' : 'text-farm-400 hover:bg-farm-800')}
          aria-label="AMS environment data" aria-pressed={activePanel === 'ams'}>
          <Thermometer size={14} /> <span className="hidden sm:inline">AMS</span>
        </button>
        <button onClick={() => setActivePanel(activePanel === 'telemetry' ? null : 'telemetry')}
          className={clsx('p-1.5 rounded-lg text-xs flex items-center gap-1 transition-colors',
            activePanel === 'telemetry' ? 'bg-print-600/20 text-print-400' : 'text-farm-400 hover:bg-farm-800')}
          aria-label="Print telemetry" aria-pressed={activePanel === 'telemetry'}>
          <Activity size={14} /> <span className="hidden sm:inline">Telemetry</span>
        </button>
        <button onClick={() => setActivePanel(activePanel === 'nozzle' ? null : 'nozzle')}
          className={clsx('p-1.5 rounded-lg text-xs flex items-center gap-1 transition-colors',
            activePanel === 'nozzle' ? 'bg-print-600/20 text-print-400' : 'text-farm-400 hover:bg-farm-800')}
          aria-label="Nozzle lifecycle" aria-pressed={activePanel === 'nozzle'}>
          <CircleDot size={14} /> <span className="hidden sm:inline">Nozzle</span>
        </button>
        <button onClick={() => setActivePanel(activePanel === 'hms' ? null : 'hms')}
          className={clsx('p-1.5 rounded-lg text-xs flex items-center gap-1 transition-colors',
            activePanel === 'hms' ? 'bg-print-600/20 text-print-400' : 'text-farm-400 hover:bg-farm-800')}
          aria-label="HMS error history" aria-pressed={activePanel === 'hms'}>
          <AlertTriangle size={14} /> <span className="hidden sm:inline">HMS</span>
        </button>
      </div>
      {activePanel === 'ams' && <AmsEnvironmentChart printerId={printer.id} onClose={() => setActivePanel(null)} />}
      {activePanel === 'telemetry' && <PrinterTelemetryChart printerId={printer.id} onClose={() => setActivePanel(null)} />}
      {activePanel === 'nozzle' && <NozzleStatusCard printerId={printer.id} onClose={() => setActivePanel(null)} />}
      {activePanel === 'hms' && <HmsHistoryPanel printerId={printer.id} apiType={printer.api_type} onClose={() => setActivePanel(null)} />}
    </div>
  )
}
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

function PrinterModal({ isOpen, onClose, onSubmit, printer, onSyncAms }) {
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

export default function Printers() {
  const [deleteConfirmId, setDeleteConfirmId] = useState(null)
  const [cameraTarget, setCameraTarget] = useState(null)
  const { data: activeCameras } = useQuery({
    queryKey: ['cameras'],
    queryFn: () => printers.getCameras().catch(() => [])
  })
  const cameraIds = new Set((activeCameras || []).map(c => c.id))
  const queryClient = useQueryClient()
  const [showModal, setShowModal] = useState(false)
  const [showUpgradeModal, setShowUpgradeModal] = useState(false)
  const [plugStates, setPlugStates] = useState({})

  // Load plug states for printers that have plugs

  const handlePlugToggle = async (printerId) => {
    const isOn = plugStates[printerId]
    try {
      if (isOn) {
        await plugPowerOff(printerId)
      } else {
        await plugPowerOn(printerId)
      }
      setPlugStates(prev => ({ ...prev, [printerId]: !isOn }))
    } catch (e) {
      console.error('Plug toggle failed:', e)
    }
  }
  const [editingPrinter, setEditingPrinter] = useState(null)
  const [cardSize, setCardSize] = useState(() => localStorage.getItem('printerCardSize') || 'M')
  const [orderedPrinters, setOrderedPrinters] = useState([])
  const [draggedId, setDraggedId] = useState(null)
  const [showScanner, setShowScanner] = useState(false)
  const [scannerPrinterId, setScannerPrinterId] = useState(null)
  const [searchTerm, setSearchTerm] = useState(() => sessionStorage.getItem('printers_search') || '')
  const [statusFilter, setStatusFilter] = useState(() => sessionStorage.getItem('printers_status') || 'all')
  const [typeFilter, setTypeFilter] = useState(() => sessionStorage.getItem('printers_type') || 'all')
  const [sortBy, setSortBy] = useState(() => sessionStorage.getItem('printers_sort') || 'manual')
  const [tagFilter, setTagFilter] = useState('')

  const org = useOrg()
  const { data: printersData, isLoading } = useQuery({ queryKey: ['printers', org.orgId], queryFn: () => printers.list(false, '', org.orgId) })
  const lic = useLicense()
  const atLimit = !lic.isPro && (printersData?.length || 0) >= 5
  const { data: filamentsData } = useQuery({ queryKey: ['filaments-combined'], queryFn: () => filaments.combined() })
  const { data: spoolsData } = useQuery({ queryKey: ['spools'], queryFn: () => spoolsApi.list({ status: 'active' }) })
  
  const createPrinter = useMutation({ mutationFn: printers.create, onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['printers'] }); setShowModal(false) } })
  const updatePrinter = useMutation({ mutationFn: ({ id, data }) => printers.update(id, data), onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['printers'] }); setShowModal(false); setEditingPrinter(null) } })
  const deletePrinter = useMutation({ mutationFn: printers.delete, onSuccess: () => queryClient.invalidateQueries({ queryKey: ['printers'] }) })
  const updateSlot = useMutation({ mutationFn: ({ printerId, slotNumber, data }) => printers.updateSlot(printerId, slotNumber, data), onSuccess: () => queryClient.invalidateQueries({ queryKey: ['printers'] }) })
  const reorderPrinters = useMutation({ mutationFn: printers.reorder, onSuccess: () => queryClient.invalidateQueries({ queryKey: ['printers'] }) })

  // Bulk selection
  const [selectedPrinters, setSelectedPrinters] = useState(new Set())
  const togglePrinterSelect = (id) => setSelectedPrinters(prev => {
    const next = new Set(prev)
    next.has(id) ? next.delete(id) : next.add(id)
    return next
  })
  const toggleSelectAllPrinters = (ids) => {
    setSelectedPrinters(prev => prev.size === ids.length ? new Set() : new Set(ids))
  }
  const bulkPrinterAction = useMutation({
    mutationFn: ({ action, extra }) => bulkOps.printers([...selectedPrinters], action, extra),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['printers'] }); setSelectedPrinters(new Set()) },
  })

  useEffect(() => {
    if (printersData) setOrderedPrinters(printersData)
  }, [printersData])
  useEffect(() => {
    if (printersData) {
      printersData.filter(p => p.plug_type).forEach(async (p) => {
        try {
          const state = await getPlugState(p.id)
          setPlugStates(prev => ({ ...prev, [p.id]: state?.is_on || false }))
        } catch (e) {}
      })
    }
  }, [printersData])

  // Persist filter state to sessionStorage
  useEffect(() => { sessionStorage.setItem('printers_search', searchTerm) }, [searchTerm])
  useEffect(() => { sessionStorage.setItem('printers_status', statusFilter) }, [statusFilter])
  useEffect(() => { sessionStorage.setItem('printers_type', typeFilter) }, [typeFilter])
  useEffect(() => { sessionStorage.setItem('printers_sort', sortBy) }, [sortBy])

  // Derive unique api_types from printers
  const apiTypes = [...new Set((printersData || []).map(p => p.api_type).filter(Boolean))]

  const allTags = [...new Set((printersData || []).flatMap(p => p.tags || []))].sort()
  const isFiltered = searchTerm || statusFilter !== 'all' || typeFilter !== 'all' || sortBy !== 'manual' || tagFilter

  const filteredPrinters = (() => {
    let list = [...(orderedPrinters || [])]
    if (searchTerm) {
      const q = searchTerm.toLowerCase()
      list = list.filter(p =>
        (p.name || '').toLowerCase().includes(q) ||
        (p.nickname || '').toLowerCase().includes(q) ||
        (p.model || '').toLowerCase().includes(q)
      )
    }
    if (statusFilter !== 'all') {
      list = list.filter(p => {
        const online = p.last_seen && (Date.now() - new Date(p.last_seen + 'Z').getTime()) < 90000
        const stage = p.print_stage && p.print_stage !== 'Idle' ? p.print_stage : null
        switch (statusFilter) {
          case 'online': return online
          case 'offline': return !online
          case 'printing': return stage === 'Running' || stage === 'Printing'
          case 'idle': return online && (!stage || stage === 'Idle')
          default: return true
        }
      })
    }
    if (typeFilter !== 'all') {
      list = list.filter(p => p.api_type === typeFilter)
    }
    if (tagFilter) {
      list = list.filter(p => p.tags?.includes(tagFilter))
    }
    if (sortBy !== 'manual') {
      list.sort((a, b) => {
        switch (sortBy) {
          case 'name_asc': return (a.nickname || a.name || '').localeCompare(b.nickname || b.name || '')
          case 'name_desc': return (b.nickname || b.name || '').localeCompare(a.nickname || a.name || '')
          case 'status': {
            const aOn = a.last_seen && (Date.now() - new Date(a.last_seen + 'Z').getTime()) < 90000 ? 0 : 1
            const bOn = b.last_seen && (Date.now() - new Date(b.last_seen + 'Z').getTime()) < 90000 ? 0 : 1
            return aOn - bOn
          }
          case 'model': return (a.model || '').localeCompare(b.model || '')
          default: return 0
        }
      })
    }
    return list
  })()

  const handleDragStart = (e, printerId) => {
    setDraggedId(printerId)
    e.dataTransfer.effectAllowed = 'move'
  }

  const handleDragOver = (e, targetId) => {
    e.preventDefault()
    if (draggedId === null || draggedId === targetId) return
    const draggedIndex = orderedPrinters.findIndex(p => p.id === draggedId)
    const targetIndex = orderedPrinters.findIndex(p => p.id === targetId)
    if (draggedIndex === targetIndex) return
    const newOrder = [...orderedPrinters]
    const [dragged] = newOrder.splice(draggedIndex, 1)
    newOrder.splice(targetIndex, 0, dragged)
    setOrderedPrinters(newOrder)
  }

  const handleDragEnd = () => {
    if (draggedId !== null) reorderPrinters.mutate(orderedPrinters.map(p => p.id))
    setDraggedId(null)
  }

  const handleSubmit = (data, printerId) => {
    if (printerId) {
      updatePrinter.mutate({ id: printerId, data })
    } else {
      createPrinter.mutate(data)
    }
  }

  const handleEdit = (printer) => {
    setEditingPrinter(printer)
    setShowModal(true)
  }

  const handleCloseModal = () => {
    setShowModal(false)
    setEditingPrinter(null)
  }

  const handleSyncAms = async (printerId) => {
    try {
      await printers.syncAms(printerId)
      queryClient.invalidateQueries({ queryKey: ['printers'] })
    } catch (err) {
      toast.error('Failed to sync AMS')
    }
  }

  return (
    <div className="p-4 md:p-6">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-4 md:mb-6">
        <div className="flex items-center gap-3">
          <PrinterIcon className="text-print-400" size={24} />
          <div>
            <h1 className="text-xl md:text-2xl font-display font-bold">Printers</h1>
            <p className="text-farm-500 text-sm mt-1">Manage your print farm</p>
          </div>
        </div>
        {canDo('printers.add') && (atLimit
          ? <button onClick={() => setShowUpgradeModal(true)} className="flex items-center gap-2 px-4 py-2 bg-farm-700 text-farm-400 hover:text-farm-300 rounded-lg text-sm self-start transition-colors" title={`Printer limit reached (${lic.max_printers || 5}). Upgrade to Pro for unlimited.`}>
              <Plus size={16} /> Add Printer (limit: {lic.max_printers || 5})
            </button>
          : <button onClick={() => setShowModal(true)} className="flex items-center gap-2 px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg transition-colors text-sm self-start">
              <Plus size={16} /> Add Printer
            </button>
        )}
      </div>
      {/* Filter Toolbar */}
      {printersData?.length > 0 && (
        <div className="bg-farm-900 border border-farm-800 rounded-lg p-3 mb-4 md:mb-6 flex flex-wrap items-center gap-3">
          <div className="relative flex-1 min-w-[180px] max-w-xs">
            <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-farm-500" />
            <input
              type="text"
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              placeholder="Search printers..."
              className="w-full bg-farm-800 border border-farm-700 rounded-lg pl-8 pr-3 py-1.5 text-sm"
            />
          </div>
          <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} className="bg-farm-800 border border-farm-700 rounded-lg px-2 py-1.5 text-sm">
            <option value="all">All Status</option>
            <option value="online">Online</option>
            <option value="offline">Offline</option>
            <option value="printing">Printing</option>
            <option value="idle">Idle</option>
          </select>
          {apiTypes.length > 1 && (
            <select value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)} className="bg-farm-800 border border-farm-700 rounded-lg px-2 py-1.5 text-sm">
              <option value="all">All Types</option>
              {apiTypes.map(t => (
                <option key={t} value={t}>{t === 'bambu' ? 'Bambu' : t === 'moonraker' ? 'Moonraker' : t === 'prusalink' ? 'PrusaLink' : t === 'elegoo' ? 'Elegoo' : t}</option>
              ))}
            </select>
          )}
          {allTags.length > 0 && (
            <select value={tagFilter} onChange={(e) => setTagFilter(e.target.value)} className="bg-farm-800 border border-farm-700 rounded-lg px-2 py-1.5 text-sm">
              <option value="">All Tags</option>
              {allTags.map(t => <option key={t} value={t}>{t}</option>)}
            </select>
          )}
          <select value={sortBy} onChange={(e) => setSortBy(e.target.value)} className="bg-farm-800 border border-farm-700 rounded-lg px-2 py-1.5 text-sm">
            <option value="manual">Manual Order</option>
            <option value="name_asc">Name A-Z</option>
            <option value="name_desc">Name Z-A</option>
            <option value="status">Status (online first)</option>
            <option value="model">Model</option>
          </select>
          {isFiltered && (
            <span className="text-xs text-farm-400">
              Showing {filteredPrinters.length} of {orderedPrinters.length} printers
            </span>
          )}
          <div className="flex items-center gap-0.5 ml-auto border border-farm-700 rounded-lg overflow-hidden">
            {['S', 'M', 'L', 'XL'].map(size => (
              <button
                key={size}
                onClick={() => { setCardSize(size); localStorage.setItem('printerCardSize', size) }}
                className={`px-2 py-1.5 text-xs font-medium transition-colors ${cardSize === size ? 'bg-print-600 text-white' : 'bg-farm-800 text-farm-400 hover:bg-farm-700'}`}
              >
                {size}
              </button>
            ))}
          </div>
        </div>
      )}

      {isLoading ? (
        <div className="text-center py-12 text-farm-500 text-sm">Loading printers...</div>
      ) : printersData?.length === 0 ? (
        <div className="bg-farm-900 rounded-lg border border-farm-800 p-8 md:p-12 text-center">
          <p className="text-farm-500 mb-4">No printers configured yet.</p>
          {canDo('printers.add') && !atLimit && <button onClick={() => setShowModal(true)} className="px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg transition-colors text-sm">Add Your First Printer</button>}
        </div>
      ) : (
        <>
        {selectedPrinters.size > 0 && canDo('printers.edit') && (
          <div className="flex items-center gap-3 mb-4 p-3 bg-print-900/50 border border-print-700 rounded-lg">
            <span className="text-sm text-farm-300">{selectedPrinters.size} selected</span>
            <button onClick={() => bulkPrinterAction.mutate({ action: 'enable' })} className="px-3 py-1 bg-green-600 hover:bg-green-500 rounded text-xs">Enable</button>
            <button onClick={() => bulkPrinterAction.mutate({ action: 'disable' })} className="px-3 py-1 bg-amber-600 hover:bg-amber-500 rounded text-xs">Disable</button>
            <button onClick={() => setSelectedPrinters(new Set())} className="px-3 py-1 bg-farm-700 hover:bg-farm-600 rounded text-xs">Clear</button>
          </div>
        )}
        {canDo('printers.edit') && filteredPrinters.length > 0 && (
          <div className="flex items-center gap-2 mb-3">
            <label className="flex items-center gap-1.5 text-xs text-farm-400 cursor-pointer">
              <input type="checkbox" checked={selectedPrinters.size === filteredPrinters.length && filteredPrinters.length > 0} onChange={() => toggleSelectAllPrinters(filteredPrinters.map(p => p.id))} className="rounded border-farm-600" />
              Select all
            </label>
          </div>
        )}
        <div className={clsx('grid gap-4 md:gap-6 items-start', {
          'grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4': cardSize === 'S',
          'grid-cols-1 lg:grid-cols-2 xl:grid-cols-3': cardSize === 'M',
          'grid-cols-1 lg:grid-cols-2': cardSize === 'L',
          'grid-cols-1': cardSize === 'XL',
        })}>
          {filteredPrinters.map((printer) => (
            <div key={printer.id} className="relative">
              {canDo('printers.edit') && (
                <input
                  type="checkbox"
                  checked={selectedPrinters.has(printer.id)}
                  onChange={() => togglePrinterSelect(printer.id)}
                  className="absolute top-3 left-3 z-10 rounded border-farm-600"
                />
              )}
              <PrinterCard
                printer={printer}
                allFilaments={filamentsData}
                spools={spoolsData}
                onDelete={(id) => setDeleteConfirmId(id)}
                onToggleActive={(id, active) => updatePrinter.mutate({ id, data: { is_active: active } })}
                onUpdateSlot={(pid, slot, data) => updateSlot.mutate({ printerId: pid, slotNumber: slot, data })}
                onEdit={handleEdit}
                onSyncAms={handleSyncAms}
                hasCamera={cameraIds.has(printer.id)}
                onCameraClick={setCameraTarget}
                isDragging={draggedId === printer.id}
                onDragStart={isFiltered ? undefined : (e) => handleDragStart(e, printer.id)}
                onDragOver={isFiltered ? undefined : (e) => handleDragOver(e, printer.id)}
                onDragEnd={isFiltered ? undefined : handleDragEnd}
                onScanSpool={() => { setScannerPrinterId(printer.id); setShowScanner(true); }}
                onPlugToggle={handlePlugToggle}
                plugStates={plugStates}
              />
            </div>
          ))}
          {filteredPrinters.length === 0 && orderedPrinters.length > 0 && (
            <div className="col-span-full bg-farm-900 rounded-lg border border-farm-800 p-8 text-center">
              <p className="text-farm-500 text-sm">No printers match your filters.</p>
            </div>
          )}
        </div>
        </>
      )}
      <PrinterModal isOpen={showModal} onClose={handleCloseModal} onSubmit={handleSubmit} printer={editingPrinter} />
      {cameraTarget && <CameraModal printer={cameraTarget} onClose={() => setCameraTarget(null)} />}
      {showScanner && (
        <QRScannerModal
          isOpen={showScanner}
          onClose={() => setShowScanner(false)}
          preselectedPrinter={scannerPrinterId}
          onAssigned={() => {
            setShowScanner(false);
            queryClient.invalidateQueries({ queryKey: ['printers'] });
          }}
        />
      )}
      <UpgradeModal isOpen={showUpgradeModal} onClose={() => setShowUpgradeModal(false)} resource="printers" />
      <ConfirmModal
        open={!!deleteConfirmId}
        onConfirm={() => { deletePrinter.mutate(deleteConfirmId); setDeleteConfirmId(null) }}
        onCancel={() => setDeleteConfirmId(null)}
        title="Delete Printer"
        message="Permanently delete this printer? This cannot be undone."
        confirmText="Delete"
        confirmVariant="danger"
      />
    </div>
  )
}
