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
import { isOnline } from '../../utils/shared'
import { Button, SpoolRing } from '../ui'

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
        "bg-[var(--brand-card-bg)] rounded-md border-0 overflow-hidden h-fit transition-all hover:brightness-110",
        isDragging && "border border-print-500 opacity-50 scale-95"
      )}
      style={{ boxShadow: 'var(--brand-card-shadow)' }}
      draggable={!!onDragStart}
      onDragStart={onDragStart}
      onDragOver={onDragOver}
      onDragEnd={onDragEnd}
    >
      <div className="p-3 md:p-4 border-b border-[var(--brand-border)] flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 md:gap-3 min-w-0">
          <div className="cursor-grab active:cursor-grabbing text-[var(--brand-text-muted)] hover:text-[var(--brand-text-secondary)] flex-shrink-0">
            <GripVertical size={16} />
          </div>
          <div className="min-w-0">
            <h3 className="font-display font-semibold text-base md:text-lg truncate">{printer.nickname || printer.name}</h3>
            <div className="flex items-center gap-1.5 flex-wrap">
              <span className="text-xs md:text-sm text-[var(--brand-text-muted)] truncate">{printer.model || 'Unknown model'}</span>
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
          {canDo('printers.edit') && <Button variant="ghost" size="icon" icon={Settings} onClick={() => onEdit(printer)} aria-label="Edit printer settings" />}
          {canDo('printers.edit') && <Button variant="ghost" size="icon" icon={printer.is_active ? Power : PowerOff} onClick={() => onToggleActive(printer.id, !printer.is_active)} className={clsx(printer.is_active ? 'text-[var(--brand-primary)] hover:bg-[var(--brand-primary)]/10' : 'text-[var(--brand-text-muted)] hover:bg-[var(--brand-input-bg)]')} aria-label={printer.is_active ? 'Deactivate printer' : 'Activate printer'} />}
          {hasCamera && <Button variant="ghost" size="icon" icon={Video} onClick={() => onCameraClick(printer)} aria-label="View camera" />}
          <Button variant="ghost" size="icon" icon={ExternalLink} onClick={() => { navigator.clipboard.writeText(`${window.location.origin}/overlay/${printer.id}`); toast.success('Overlay URL copied') }} aria-label="Copy OBS overlay URL" title="Copy OBS overlay URL" />
          {printer.plug_type && onPlugToggle && (
            <Button
              variant="ghost"
              size="icon"
              icon={Plug}
              onClick={() => onPlugToggle(printer.id)}
              className={plugStates?.[printer.id] ? 'text-green-400 hover:bg-green-900/30' : 'text-[var(--brand-text-muted)] hover:bg-[var(--brand-input-bg)]'}
              aria-label={plugStates?.[printer.id] ? 'Power off plug' : 'Power on plug'}
            />
          )}
          {onScanSpool && <Button variant="ghost" size="icon" icon={QrCode} onClick={onScanSpool} aria-label="Scan spool QR code" />}
          {canDo('printers.delete') && <Button variant="ghost" size="icon" icon={Trash2} onClick={() => onDelete(printer.id)} className="text-[var(--brand-text-muted)] hover:text-red-400 hover:bg-red-900/50" aria-label="Delete printer" />}
        </div>
      </div>
      <div className="p-3 md:p-4">
        <div className="flex items-center gap-2 mb-3 flex-wrap">
          <Palette size={14} className="text-[var(--brand-text-muted)]" />
          <span className="text-xs md:text-sm text-[var(--brand-text-secondary)]">Loaded Filaments</span>
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
              className="ml-auto text-xs px-2 py-1 bg-[var(--brand-input-bg)] hover:brightness-110 rounded-md transition-colors disabled:opacity-50"
              title="Sync filament state from printer"
            >
              {syncing ? '⟳ Syncing...' : '↻ Sync AMS'}
            </button>
          ) : (
            <span className="text-xs text-[var(--brand-text-muted)] ml-auto">(click to edit)</span>
          )}
        </div>
        {printer.machine_type === 'H2D' && printer.filament_slots?.length > 4 && (
          <div className="flex items-center gap-2 mb-1">
            <span className="text-[10px] text-[var(--brand-text-muted)] font-medium">AMS Unit 0</span>
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
                <div key="ams1-label" className="col-span-4 text-[10px] text-[var(--brand-text-muted)] font-medium mt-1">AMS Unit 1</div>,
                el,
              ]
            }
            return el
          })}
        </div>
        {/* H2D External Spools (Ext-L / Ext-R) */}
        {printer.machine_type === 'H2D' && printer.external_spools && (
          <div className="mt-2 pt-2 border-t border-[var(--brand-border)]">
            <span className="text-[10px] text-[var(--brand-text-muted)] font-medium">External Spools</span>
            <div className="grid grid-cols-2 gap-2 mt-1">
              {['left', 'right'].map(side => {
                const ext = printer.external_spools?.[side]
                return (
                  <div key={side} className="flex items-center gap-1.5 bg-[var(--brand-input-bg)] rounded-md px-2 py-1">
                    <span className="text-[10px] text-[var(--brand-text-muted)] uppercase w-7">Ext-{side === 'left' ? 'L' : 'R'}</span>
                    {ext ? (
                      <>
                        <SpoolRing color={ext.color || '#666'} material={ext.material || ''} level={ext.remain_percent != null ? ext.remain_percent : 100} size={16} />
                        {ext.remain_percent != null && (
                          <span className="text-[10px] text-[var(--brand-text-muted)] ml-auto">{ext.remain_percent}%</span>
                        )}
                      </>
                    ) : (
                      <SpoolRing empty size={16} />
                    )}
                  </div>
                )
              })}
            </div>
          </div>
        )}
      </div>
      <div className="px-3 md:px-4 py-2 md:py-3 bg-[var(--brand-surface)] border-t border-[var(--brand-border)]">
        {(() => {
          const online = isOnline(printer)
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
                  <div className={`w-1.5 h-1.5 rounded-full ${online ? "bg-[var(--status-completed)]" : "bg-[var(--brand-text-muted)]"}`}></div>
                  <span className={online ? "text-[var(--status-completed)]" : "text-[var(--brand-text-muted)]"}>{online ? "Online" : "Offline"}</span>
                </div>
                {printer.lights_on != null && (
                  <button
                    onClick={(e) => { e.stopPropagation(); printers.toggleLights(printer.id) }}
                    className={`p-0.5 rounded-md transition-colors ${printer.lights_on ? 'text-yellow-400 hover:text-yellow-300' : 'text-[var(--brand-text-muted)] hover:text-[var(--brand-text-secondary)]'}`}
                    aria-label={printer.lights_on ? 'Turn lights off' : 'Turn lights on'}
                  >
                    <Lightbulb size={14} />
                  </button>
                )}
              </div>
              <div className="flex items-center gap-3 font-mono text-xs text-[var(--brand-text-secondary)]">
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
                        <span className={(n1tt && n1tt > 0) ? "text-orange-400" : "text-[var(--brand-text-secondary)]"} title="Right Nozzle">
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
        <div className="px-3 md:px-4 py-2 border-t border-[var(--brand-border)]">
          <div className="flex items-center gap-1">
            <span className="text-xs text-[var(--brand-text-muted)] mr-1">Speed</span>
            {[
              { level: 1, label: 'Silent', icon: '🐢' },
              { level: 2, label: 'Standard', icon: '▶' },
              { level: 3, label: 'Sport', icon: '⚡' },
              { level: 4, label: 'Ludicrous', icon: '🚀' },
            ].map(s => (
              <button
                key={s.level}
                onClick={(e) => { e.stopPropagation(); printers.setSpeed(printer.id, s.level) }}
                className="px-2 py-1 rounded-md text-xs transition-colors bg-[var(--brand-input-bg)] text-[var(--brand-text-secondary)] hover:brightness-110"
                title={s.label}
              >
                {s.icon}
              </button>
            ))}
          </div>
        </div>
      )}
      {/* Data & Diagnostics toolbar */}
      <div className="px-3 md:px-4 py-2 border-t border-[var(--brand-border)] flex items-center gap-1">
        <span className="text-xs text-[var(--brand-text-muted)] mr-1">Data</span>
        <button onClick={() => setActivePanel(activePanel === 'ams' ? null : 'ams')}
          className={clsx('p-1.5 text-xs flex items-center gap-1 transition-colors',
            activePanel === 'ams' ? 'text-[var(--brand-primary)]' : 'text-[var(--brand-text-secondary)]')}
          aria-label="AMS environment data" aria-pressed={activePanel === 'ams'}>
          <Thermometer size={14} /> <span className="hidden sm:inline">AMS</span>
        </button>
        <button onClick={() => setActivePanel(activePanel === 'telemetry' ? null : 'telemetry')}
          className={clsx('p-1.5 text-xs flex items-center gap-1 transition-colors',
            activePanel === 'telemetry' ? 'text-[var(--brand-primary)]' : 'text-[var(--brand-text-secondary)]')}
          aria-label="Print telemetry" aria-pressed={activePanel === 'telemetry'}>
          <Activity size={14} /> <span className="hidden sm:inline">Telemetry</span>
        </button>
        <button onClick={() => setActivePanel(activePanel === 'nozzle' ? null : 'nozzle')}
          className={clsx('p-1.5 text-xs flex items-center gap-1 transition-colors',
            activePanel === 'nozzle' ? 'text-[var(--brand-primary)]' : 'text-[var(--brand-text-secondary)]')}
          aria-label="Nozzle lifecycle" aria-pressed={activePanel === 'nozzle'}>
          <CircleDot size={14} /> <span className="hidden sm:inline">Nozzle</span>
        </button>
        <button onClick={() => setActivePanel(activePanel === 'hms' ? null : 'hms')}
          className={clsx('p-1.5 text-xs flex items-center gap-1 transition-colors',
            activePanel === 'hms' ? 'text-[var(--brand-primary)]' : 'text-[var(--brand-text-secondary)]')}
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
