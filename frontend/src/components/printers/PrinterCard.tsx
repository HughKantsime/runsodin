import { useState, type DragEvent } from 'react'
import { Trash2, Power, PowerOff, Palette, Settings, GripVertical, AlertTriangle, Lightbulb, Activity, CircleDot, Video, QrCode, Thermometer, Plug, ExternalLink, Turtle, Zap, Rocket } from 'lucide-react'
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

// TODO: type this properly — runtime printer shape has extra MQTT/telemetry fields
interface PrinterLike {
  id: number
  name: string
  nickname: string | null
  model: string | null
  is_active: boolean
  api_type: string | null
  api_host: string | null
  has_api_key: boolean
  camera_url: string | null
  bed_temp: number | null
  bed_target_temp: number | null
  nozzle_temp: number | null
  nozzle_target_temp: number | null
  gcode_state: string | null
  print_stage: string | null
  lights_on: boolean | null
  filament_slots: any[]
  tags: string[]
  machine_type: string | null
  plug_type?: string
  external_spools?: Record<string, any>
  h2d_nozzles?: Record<string, any>
  [key: string]: any
}

type PanelType = 'ams' | 'telemetry' | 'nozzle' | 'hms' | null

interface PrinterCardProps {
  printer: PrinterLike
  allFilaments: any[] | null
  spools: any[] | null
  onDelete: (id: number) => void
  onToggleActive: (id: number, active: boolean) => void
  onUpdateSlot: (printerId: number, slotNumber: number, data: any) => void
  onEdit: (printer: PrinterLike) => void
  onSyncAms: (printerId: number) => Promise<void>
  isDragging?: boolean
  onDragStart?: (e: DragEvent) => void
  onDragOver?: (e: DragEvent) => void
  onDragEnd?: (e: DragEvent) => void
  hasCamera?: boolean
  onCameraClick?: (printer: PrinterLike) => void
  onScanSpool?: () => void
  onPlugToggle?: (printerId: number) => void
  plugStates?: Record<number, boolean>
}

function LedgerFilamentSlot({ slot }: { slot: any }) {
  const label = slot.display_name || `Slot ${slot.slot_number}`
  const mapped = slot.mapping_status === 'mapped'
  const available = slot.mapping_status !== 'unavailable'
  const remaining = typeof slot.remaining === 'number' && Number.isFinite(slot.remaining) ? Math.max(0, Math.min(100, slot.remaining)) : null
  const color = typeof slot.color_hex === 'string' && /^[0-9a-f]{6}$/i.test(slot.color_hex) ? `#${slot.color_hex}` : '#888'
  return (
    <div aria-label={label} className="bg-[var(--brand-input-bg)] rounded-md p-2 min-w-0 text-center flex flex-col items-center gap-1">
      <span className="text-xs font-medium">{label}</span>
      {mapped ? <>
        {remaining !== null ? <SpoolRing color={color} material={slot.material_type || ''} level={remaining} size={20} /> : <span className="inline-flex items-center gap-1.5 text-xs"><span aria-label="Spool color" className="inline-block w-3 h-3 rounded-full" style={{ backgroundColor: color }} />{slot.material_type || 'Material unknown'}</span>}
        {slot.color && <span className="text-xs text-[var(--brand-text-secondary)] break-words">{slot.color}</span>}
        <span className="text-xs text-[var(--brand-text-muted)]">Mapped spool{slot.external_spool_id != null ? ` #${slot.external_spool_id}` : ''}</span>
        <span className="text-xs text-[var(--brand-text-muted)]">{remaining !== null ? `${Math.round(remaining)}% remaining` : 'Remaining amount unknown'}</span>
      </> : <span className="text-xs text-yellow-400">{available ? 'Unmapped' : 'Spool information unavailable'}</span>}
    </div>
  )
}

export default function PrinterCard({ printer, allFilaments, spools, onDelete, onToggleActive, onUpdateSlot, onEdit, onSyncAms, isDragging, onDragStart, onDragOver, onDragEnd, hasCamera, onCameraClick, onScanSpool, onPlugToggle, plugStates }: PrinterCardProps) {
  const externallyManagedFilament = printer.filament_source === 'filament-ledger'
  const [syncing, setSyncing] = useState(false)
  const [activePanel, setActivePanel] = useState<PanelType>(null)

  const handleSyncAms = async () => {
    setSyncing(true)
    try {
      await onSyncAms(printer.id)
    } finally {
      setSyncing(false)
    }
  }

  const hasBambuConnection = printer.api_type === 'bambu' && printer.api_host && printer.has_api_key

  const slotsNeedingAttention = externallyManagedFilament ? 0 : printer.filament_slots?.filter((s: any) =>
    (s.assigned_spool_id && !s.spool_confirmed) || (!s.assigned_spool_id && s.color_hex)
  ).length || 0

  return (
    <div
      data-testid="printer-card"
      data-printer-id={printer.id}
      data-printer-name={printer.name}
      className={clsx(
        "bg-[var(--brand-card-bg)] rounded-md border-0 overflow-hidden h-fit transition-all hover:brightness-110",
        isDragging && "border border-[var(--brand-primary)] opacity-50 scale-95"
      )}
      style={{ boxShadow: 'var(--brand-card-shadow)' }}
      draggable={!!onDragStart}
      onDragStart={onDragStart}
      onDragOver={onDragOver}
      onDragEnd={onDragEnd}
    >
      <div className="p-3 md:p-4 border-b border-[var(--brand-border)] flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 md:gap-3 min-w-0">
          <div className="cursor-grab active:cursor-grabbing text-[var(--brand-text-muted)] hover:text-[var(--brand-text-secondary)] shrink-0">
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
                <span key={tag} className="px-1.5 py-0.5 bg-[var(--brand-primary)]/20 text-[var(--brand-primary)] text-[10px] rounded-full border border-[var(--brand-primary)]/30">{tag}</span>
              ))}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-1 shrink-0">
          {canDo('printers.edit') && <Button variant="ghost" size="icon" icon={Settings} onClick={() => onEdit(printer)} aria-label="Edit printer settings" />}
          {canDo('printers.edit') && <Button variant="ghost" size="icon" icon={printer.is_active ? Power : PowerOff} onClick={() => onToggleActive(printer.id, !printer.is_active)} className={clsx(printer.is_active ? 'text-[var(--brand-primary)] hover:bg-[var(--brand-primary)]/10' : 'text-[var(--brand-text-muted)] hover:bg-[var(--brand-input-bg)]')} aria-label={printer.is_active ? 'Deactivate printer' : 'Activate printer'} />}
          {hasCamera && <Button variant="ghost" size="icon" icon={Video} onClick={() => onCameraClick?.(printer)} aria-label="View camera" />}
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
          {!externallyManagedFilament && onScanSpool && <Button variant="ghost" size="icon" icon={QrCode} onClick={onScanSpool} aria-label="Scan spool QR code" />}
          {canDo('printers.delete') && <Button variant="ghost" size="icon" icon={Trash2} onClick={() => onDelete(printer.id)} className="text-[var(--brand-text-muted)] hover:text-red-400 hover:bg-red-900/50" aria-label="Delete printer" />}
        </div>
      </div>
      <div className="p-3 md:p-4">
        <div className="flex items-center gap-2 mb-3 flex-wrap">
          <Palette size={14} className="text-[var(--brand-text-muted)]" />
          <span className="text-xs md:text-sm text-[var(--brand-text-secondary)]">{externallyManagedFilament ? 'Filament Ledger · read only' : 'Loaded Filaments'}</span>
          {slotsNeedingAttention > 0 && (
            <span className="flex items-center gap-1 text-xs text-yellow-400" title="Slots need spool assignment">
              <AlertTriangle size={12} />
              {slotsNeedingAttention}
            </span>
          )}
          {!externallyManagedFilament && canDo('printers.slots') && hasBambuConnection ? (
            <button
              onClick={handleSyncAms}
              disabled={syncing}
              className="ml-auto text-xs px-2 py-1 bg-[var(--brand-input-bg)] hover:brightness-110 rounded-md transition-colors disabled:opacity-50"
              title="Sync filament state from printer"
            >
              {syncing ? '\u27F3 Syncing...' : '\u21BB Sync AMS'}
            </button>
          ) : (
            <span className="text-xs text-[var(--brand-text-muted)] ml-auto">{externallyManagedFilament ? 'Manage spools in Filament Ledger' : '(click to edit)'}</span>
          )}
        </div>
        {externallyManagedFilament && printer.filament_source_status === 'unavailable' && <p role="status" className="text-xs text-yellow-400 mb-3">Filament Ledger is unavailable. Spool information could not be read.</p>}
        {externallyManagedFilament && printer.filament_source_status === 'partial' && <p role="status" className="text-xs text-yellow-400 mb-3">Some slots could not be matched or read from Filament Ledger.</p>}
        {printer.machine_type === 'H2D' && printer.filament_slots?.length > 4 && (
          <div className="flex items-center gap-2 mb-1">
            <span className="text-[10px] text-[var(--brand-text-muted)] font-medium">AMS Unit 0</span>
          </div>
        )}
        <div className={clsx(
          "grid gap-2",
          printer.filament_slots?.length <= 4 ? "grid-cols-4" : "grid-cols-4"
        )}>
          {printer.filament_slots?.map((slot: any, idx: number) => {
            const el = externallyManagedFilament ? <LedgerFilamentSlot key={slot.slot_number} slot={slot} /> : (
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
        {!externallyManagedFilament && printer.machine_type === 'H2D' && printer.external_spools && (
          <div className="mt-2 pt-2 border-t border-[var(--brand-border)]">
            <span className="text-[10px] text-[var(--brand-text-muted)] font-medium">External Spools</span>
            <div className="grid grid-cols-2 gap-2 mt-1">
              {(['left', 'right'] as const).map(side => {
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
                  <span
                    data-testid="printer-status"
                    className={online ? "text-[var(--status-completed)]" : "text-[var(--brand-text-muted)]"}
                  >
                    {online ? "Online" : "Offline"}
                  </span>
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
                  <span
                    data-testid="printer-nozzle-temp"
                    className={isHeating ? "text-orange-400" : ""}
                    title={nozTarget && nozTarget > 0 ? `Nozzle: ${nozTemp}°/${nozTarget}°C` : `Nozzle: ${nozTemp}°C`}
                  >
                    Nozzle {nozTemp}°{nozTarget && nozTarget > 0 ? `/${nozTarget}°` : ''}
                  </span>
                )}
                {printer.machine_type === 'H2D' && nozTemp != null && (
                  <>
                    <span className={isHeating ? "text-orange-400" : ""} title="Left Nozzle">
                      L {nozTemp}°{nozTarget && nozTarget > 0 ? `/${nozTarget}°` : ''}
                    </span>
                    {(() => {
                      const n1 = printer.h2d_nozzles?.nozzle_1
                      const n1t = n1?.temp != null ? Math.round(n1.temp) : null
                      const n1tt = n1?.target != null ? Math.round(n1.target) : null
                      return (
                        <span className={(n1tt && n1tt > 0) ? "text-orange-400" : "text-[var(--brand-text-secondary)]"} title="Right Nozzle">
                          R {n1t != null ? `${n1t}°${n1tt && n1tt > 0 ? `/${n1tt}°` : ''}` : '—'}
                        </span>
                      )
                    })()}
                  </>
                )}
                {bedTemp != null && (
                  <span
                    data-testid="printer-bed-temp"
                    className={bedTarget && bedTarget > 0 ? "text-orange-400" : ""}
                    title={bedTarget && bedTarget > 0 ? `Bed: ${bedTemp}°/${bedTarget}°C` : `Bed: ${bedTemp}°C`}
                  >
                    Bed {bedTemp}°{bedTarget && bedTarget > 0 ? `/${bedTarget}°` : ''}
                  </span>
                )}
                {stage && (
                  <span className="text-[var(--brand-primary)]">{stage}</span>
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
              { level: 1, label: 'Silent', Icon: Turtle },
              { level: 2, label: 'Standard', Icon: null },
              { level: 3, label: 'Sport', Icon: Zap },
              { level: 4, label: 'Ludicrous', Icon: Rocket },
            ].map(s => (
              <button
                key={s.level}
                onClick={(e) => { e.stopPropagation(); printers.setSpeed(printer.id, s.level) }}
                className="px-2 py-1 rounded-md text-xs transition-colors bg-[var(--brand-input-bg)] text-[var(--brand-text-secondary)] hover:brightness-110"
                title={s.label}
              >
                {s.Icon ? <s.Icon size={12} /> : '\u25B6'}
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
