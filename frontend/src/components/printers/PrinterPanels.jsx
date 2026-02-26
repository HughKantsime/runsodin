import { Thermometer, Wind, Activity } from 'lucide-react'
import { getShortName } from '../../utils/shared'

export function PrinterInfoPanel({ printer }) {
  const online = printer.last_seen && (Date.now() - new Date(printer.last_seen + 'Z').getTime()) < 90000
  const nozTemp = printer.nozzle_temp != null ? Math.round(printer.nozzle_temp) : null
  const nozTarget = printer.nozzle_target_temp != null ? Math.round(printer.nozzle_target_temp) : null
  const bedTemp = printer.bed_temp != null ? Math.round(printer.bed_temp) : null
  const bedTarget = printer.bed_target_temp != null ? Math.round(printer.bed_target_temp) : null
  const fanSpeed = printer.fan_speed != null ? Math.round(printer.fan_speed) : null
  const stage = printer.print_stage && printer.print_stage !== 'Idle' ? printer.print_stage : null

  return (
    <div className="bg-farm-900 rounded-lg border border-farm-800 p-4">
      <h3 className="text-sm font-medium text-farm-400 mb-3">Printer Status</h3>
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <span className="text-sm text-farm-400">Status</span>
          <div className="flex items-center gap-2">
            <div className={`w-2 h-2 rounded-full ${online ? 'bg-green-500' : 'bg-farm-600'}`} />
            <span className={`text-sm ${online ? 'text-green-400' : 'text-farm-500'}`}>{online ? 'Online' : 'Offline'}</span>
          </div>
        </div>
        {stage && (
          <div className="flex items-center justify-between">
            <span className="text-sm text-farm-400">Stage</span>
            <span className="text-sm text-print-400">{stage}</span>
          </div>
        )}
        {nozTemp != null && (
          <div className="flex items-center justify-between">
            <span className="text-sm text-farm-400 flex items-center gap-1.5"><Thermometer size={13} /> Nozzle</span>
            <span className={`text-sm ${nozTarget > 0 ? 'text-orange-400' : 'text-farm-300'}`}>
              {nozTemp}°{nozTarget > 0 ? `/${nozTarget}°C` : 'C'}
            </span>
          </div>
        )}
        {bedTemp != null && (
          <div className="flex items-center justify-between">
            <span className="text-sm text-farm-400 flex items-center gap-1.5"><Thermometer size={13} /> Bed</span>
            <span className={`text-sm ${bedTarget > 0 ? 'text-orange-400' : 'text-farm-300'}`}>
              {bedTemp}°{bedTarget > 0 ? `/${bedTarget}°C` : 'C'}
            </span>
          </div>
        )}
        {fanSpeed != null && (
          <div className="flex items-center justify-between">
            <span className="text-sm text-farm-400 flex items-center gap-1.5"><Wind size={13} /> Fan</span>
            <span className="text-sm text-farm-300">{fanSpeed}%</span>
          </div>
        )}
      </div>
    </div>
  )
}

export function FilamentSlotsPanel({ printer }) {
  const slots = printer.filament_slots || []
  if (!slots.length) return null

  return (
    <div className="bg-farm-900 rounded-lg border border-farm-800 p-4">
      <h3 className="text-sm font-medium text-farm-400 mb-3">Filament</h3>
      <div className="space-y-2">
        {slots.map((slot, idx) => (
          <div key={idx} className="flex items-center gap-3">
            <div
              className="w-4 h-4 rounded-lg flex-shrink-0"
              style={{ backgroundColor: slot.color_hex ? `#${slot.color_hex}` : (slot.color ? '#888' : '#333') }}
            />
            <span className="text-sm text-farm-300 flex-1 truncate">{getShortName(slot)}</span>
            {slot.remaining_weight != null && (
              <span className={`text-xs ${slot.remaining_weight < 100 ? 'text-amber-400' : 'text-farm-500'}`}>
                {Math.round(slot.remaining_weight)}g
              </span>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

export function ActiveJobPanel({ printer }) {
  const isPrinting = printer.gcode_state === 'RUNNING' || printer.gcode_state === 'PAUSE'
  if (!isPrinting) return null

  const progress = printer.mc_percent || 0
  const remaining = printer.mc_remaining_time
  const jobName = printer.gcode_file || 'Printing'
  const currentLayer = printer.layer_num
  const totalLayers = printer.total_layer_num

  const formatTime = (minutes) => {
    if (!minutes) return ''
    if (minutes < 60) return `${Math.round(minutes)}m left`
    return `${Math.floor(minutes / 60)}h ${Math.round(minutes % 60)}m left`
  }

  return (
    <div className="bg-farm-900 rounded-lg border border-farm-800 p-4">
      <h3 className="text-sm font-medium text-farm-400 mb-3 flex items-center gap-1.5">
        <Activity size={13} className="text-print-400 animate-pulse" /> Current Job
      </h3>
      <p className="text-sm font-medium mb-3 truncate">{jobName}</p>
      <div className="flex items-center justify-between mb-2">
        <span className="text-2xl font-bold text-green-400">{progress}%</span>
        {remaining && <span className="text-xs text-farm-500">{formatTime(remaining)}</span>}
      </div>
      <div className="w-full bg-farm-700 rounded-full h-2.5 mb-2">
        <div className="bg-green-500 h-2.5 rounded-full transition-all duration-500" style={{ width: `${progress}%` }} />
      </div>
      {currentLayer && totalLayers && (
        <p className="text-xs text-farm-500">Layer {currentLayer}/{totalLayers}</p>
      )}
    </div>
  )
}
