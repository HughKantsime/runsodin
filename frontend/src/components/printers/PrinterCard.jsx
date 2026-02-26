import { useState } from 'react'
import { Trash2, Power, PowerOff, Palette, Settings, GripVertical, AlertTriangle, Lightbulb, Activity, CircleDot, Video, QrCode, Thermometer, Plug, ExternalLink } from 'lucide-react'
import clsx from 'clsx'
import toast from 'react-hot-toast'
import AmsEnvironmentChart from './AmsEnvironmentChart'
import PrinterTelemetryChart from './PrinterTelemetryChart'
import NozzleStatusCard from './NozzleStatusCard'
import HmsHistoryPanel from './HmsHistoryPanel'
import FilamentSlotEditor from './FilamentSlotEditor'
import { printers } from '../../api'
import { canDo } from '../../permissions'

export default function PrinterCard({ printer, allFilaments, spools, onDelete, onToggleActive, onUpdateSlot, onEdit, onSyncAms, isDragging, onDragStart, onDragOver, onDragEnd, hasCamera, onCameraClick, onScanSpool, onPlugToggle, plugStates }) {
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
              {printer.machine_type === 'H2D' && (
                <span className="px-1.5 py-0.5 bg-purple-600/20 text-purple-400 text-[10px] rounded-full border border-purple-600/30 font-medium">H2D</span>
              )}
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
        {printer.machine_type === 'H2D' && printer.filament_slots?.length > 4 && (
          <div className="flex items-center gap-2 mb-1">
            <span className="text-[10px] text-farm-500 font-medium">AMS Unit 0</span>
          </div>
        )}
        <div className={clsx(
          "grid gap-2",
          printer.filament_slots?.length <= 4 ? "grid-cols-4" : "grid-cols-4"
        )}>
          {printer.filament_slots?.map((slot, idx) => {
            const el = (
              <FilamentSlotEditor
                printerId={printer.id}
                spools={spools}
                key={slot.id}
                slot={slot}
                allFilaments={allFilaments}
                onSave={(data) => onUpdateSlot(printer.id, slot.slot_number, data)}
              />
            )
            if (printer.machine_type === 'H2D' && idx === 4) {
              return [
                <div key="ams1-label" className="col-span-4 text-[10px] text-farm-500 font-medium mt-1">AMS Unit 1</div>,
                el,
              ]
            }
            return el
          })}
        </div>
        {/* H2D External Spools (Ext-L / Ext-R) */}
        {printer.machine_type === 'H2D' && printer.external_spools && (
          <div className="mt-2 pt-2 border-t border-farm-800/50">
            <span className="text-[10px] text-farm-500 font-medium">External Spools</span>
            <div className="grid grid-cols-2 gap-2 mt-1">
              {['left', 'right'].map(side => {
                const ext = printer.external_spools?.[side]
                return (
                  <div key={side} className="flex items-center gap-1.5 bg-farm-800/50 rounded px-2 py-1">
                    <span className="text-[10px] text-farm-500 uppercase w-7">Ext-{side === 'left' ? 'L' : 'R'}</span>
                    {ext ? (
                      <>
                        <div className="w-3 h-3 rounded-full border border-farm-600" style={{ backgroundColor: ext.color || '#666' }} />
                        <span className="text-[10px] text-farm-300">{ext.material}</span>
                        {ext.remain_percent != null && (
                          <span className="text-[10px] text-farm-500 ml-auto">{ext.remain_percent}%</span>
                        )}
                      </>
                    ) : (
                      <span className="text-[10px] text-farm-600">Empty</span>
                    )}
                  </div>
                )
              })}
            </div>
          </div>
        )}
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
                {nozTemp != null && printer.machine_type !== 'H2D' && (
                  <span className={isHeating ? "text-orange-400" : ""} title={nozTarget > 0 ? `Nozzle: ${nozTemp}°/${nozTarget}°C` : `Nozzle: ${nozTemp}°C`}>
                    Nozzle {nozTemp}°{nozTarget > 0 ? `/${nozTarget}°` : ''}
                  </span>
                )}
                {printer.machine_type === 'H2D' && nozTemp != null && (
                  <>
                    <span className={isHeating ? "text-orange-400" : ""} title="Left Nozzle">
                      L {nozTemp}°{nozTarget > 0 ? `/${nozTarget}°` : ''}
                    </span>
                    {(() => {
                      const n1 = printer.h2d_nozzles?.nozzle_1
                      const n1t = n1?.temp != null ? Math.round(n1.temp) : null
                      const n1tt = n1?.target != null ? Math.round(n1.target) : null
                      return (
                        <span className={(n1tt && n1tt > 0) ? "text-orange-400" : "text-farm-400"} title="Right Nozzle">
                          R {n1t != null ? `${n1t}°${n1tt > 0 ? `/${n1tt}°` : ''}` : '—'}
                        </span>
                      )
                    })()}
                  </>
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
