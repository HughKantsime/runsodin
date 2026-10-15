import { useState, useEffect, useRef, type DragEvent } from 'react'
import { Trash2, Power, PowerOff, Palette, Settings, GripVertical, AlertTriangle, Lightbulb, Activity, CircleDot, Video, QrCode, Thermometer, Plug, ExternalLink } from 'lucide-react'
import clsx from 'clsx'
import toast from 'react-hot-toast'
import AmsEnvironmentChart from './AmsEnvironmentChart'
import PrinterTelemetryChart from './PrinterTelemetryChart'
import NozzleStatusCard from './NozzleStatusCard'
import HmsHistoryPanel from './HmsHistoryPanel'
import FilamentSlotEditor from './FilamentSlotEditor'
import PrintProgress from './PrintProgress'
import { printers, apiTokens } from '../../api'
import { canDo, getCurrentUser } from '../../permissions'
import { canControlPlug } from '../../utils/plugControl'
import { isOnline, isActivePrintState } from '../../utils/shared'
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
  camera_enabled?: boolean
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
  plugStates?: Record<number, boolean | undefined>
  plugPending?: boolean
  plugOutcome?: string
}

/**
 * Copy the OBS overlay URL. The overlay page is public; its camera stream is
 * not, so for a printer with a camera the URL carries a new read:cameras API
 * token (revocable in Settings > API Tokens).
 */
async function copyOverlayUrl(printer: { id: number; name: string; nickname?: string | null }, withCamera: boolean) {
  const base = `${window.location.origin}/overlay/${printer.id}`
  if (!withCamera) {
    await navigator.clipboard.writeText(base)
    toast.success('Overlay URL copied')
    return
  }
  try {
    const created = await apiTokens.create({ name: `OBS overlay: ${printer.nickname || printer.name} (#${printer.id})`, scopes: ['read:cameras'] })
    await navigator.clipboard.writeText(`${base}?token=${encodeURIComponent(created.token)}`)
    toast.success('Overlay URL copied. It includes a camera token; revoke it in Settings > API Tokens.')
  } catch {
    await navigator.clipboard.writeText(base)
    toast.error('Overlay URL copied without the camera: a camera token could not be created')
  }
}

export default function PrinterCard({ printer, allFilaments, spools, onDelete, onToggleActive, onUpdateSlot, onEdit, onSyncAms, isDragging, onDragStart, onDragOver, onDragEnd, hasCamera, onCameraClick, onScanSpool, onPlugToggle, plugStates, plugPending, plugOutcome }: PrinterCardProps) {
  const [syncing, setSyncing] = useState(false)
  const [activePanel, setActivePanel] = useState<PanelType>(null)
  const [commandPending, setCommandPending] = useState(false)
  const [commandOutcome, setCommandOutcome] = useState<{ identity: string; printerId: number; text: string } | null>(null)
  const [speedDraft, setSpeedDraft] = useState<{ identity: string; printerId: number; level: number } | null>(null)
  const commandBusy = useRef(false)
  const mounted = useRef(false)
  const currentPrinter = useRef(printer)
  currentPrinter.current = printer
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  const mayControl = (target: PrinterLike, speed = false) => {
    const principal = getCurrentUser()
    if (!principal || !['operator', 'admin'].includes(principal.role) || !canDo('printers.edit')) return false
    if (target.org_id === undefined) return false
    const tenantAccess = (principal.role === 'admin' && !principal.group_id) || target.org_id === null || target.org_id === principal.group_id
    return tenantAccess && target.api_type === 'bambu' && !!target.api_host && target.has_api_key && isOnline(target)
      && (!speed || isActivePrintState(target.gcode_state))
  }

  const sendCardCommand = async (action: 'lights' | 'speed', level?: number) => {
    const target = currentPrinter.current
    if (commandBusy.current || !mayControl(target, action === 'speed') || (action === 'lights' && target.lights_on == null)) return
    const identity = JSON.stringify(getCurrentUser())
    const printerId = target.id
    const stillCurrent = () => mounted.current && currentPrinter.current.id === printerId && JSON.stringify(getCurrentUser()) === identity
    commandBusy.current = true
    setCommandPending(true)
    setCommandOutcome(null)
    try {
      const receipt = action === 'lights' ? await printers.toggleLights(printerId) : await printers.setSpeed(printerId, level!)
      const accepted = action === 'lights' ? receipt?.lights_on === !target.lights_on : receipt?.success === true
      if (stillCurrent()) setCommandOutcome({ identity, printerId, text: accepted ? 'Command accepted; printer confirmation pending' : 'Command not confirmed' })
    } catch {
      if (stillCurrent()) setCommandOutcome({ identity, printerId, text: 'Command not confirmed' })
    } finally {
      commandBusy.current = false
      if (mounted.current) setCommandPending(false)
    }
  }

  const handleSyncAms = async () => {
    setSyncing(true)
    try {
      await onSyncAms(printer.id)
    } finally {
      setSyncing(false)
    }
  }

  const hasBambuConnection = printer.api_type === 'bambu' && printer.api_host && printer.has_api_key
  const speedModes = ['Silent', 'Standard', 'Sport', 'Ludicrous']
  const reportedLevel = Number.isInteger(printer.speed_level) && printer.speed_level >= 1 && printer.speed_level <= 4 ? printer.speed_level : null
  const reportedPercent = Number.isSafeInteger(printer.speed_percent) && printer.speed_percent >= 0 ? printer.speed_percent : null
  const currentDraft = speedDraft?.identity === JSON.stringify(getCurrentUser()) && speedDraft.printerId === printer.id ? speedDraft : null
  const selectedSpeed = currentDraft?.level ?? reportedLevel ?? 2

  const slotsNeedingAttention = printer.filament_slots?.filter((s: any) =>
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
          <Button variant="ghost" size="icon" icon={ExternalLink} onClick={() => copyOverlayUrl(printer, !!hasCamera || (!!printer.camera_url && printer.camera_enabled !== false))} aria-label="Copy OBS overlay URL" title="Copy OBS overlay URL" />
          {printer.plug_type && onPlugToggle && canControlPlug(printer) && (
            <Button
              variant="ghost"
              size="icon"
              icon={Plug}
              onClick={() => { if (canControlPlug(currentPrinter.current) && !plugPending) onPlugToggle(currentPrinter.current.id) }}
              disabled={plugPending}
              className={plugStates?.[printer.id] ? 'text-green-400 hover:bg-green-900/30' : 'text-[var(--brand-text-muted)] hover:bg-[var(--brand-input-bg)]'}
              aria-label={plugStates?.[printer.id] ? 'Power off plug' : 'Power on plug'}
            />
          )}
          {onScanSpool && <Button variant="ghost" size="icon" icon={QrCode} onClick={onScanSpool} aria-label="Scan spool QR code" />}
          {canDo('printers.delete') && <Button variant="ghost" size="icon" icon={Trash2} onClick={() => onDelete(printer.id)} className="text-[var(--brand-text-muted)] hover:text-red-400 hover:bg-red-900/50" aria-label="Delete printer" />}
        </div>
      </div>
      {printer.plug_type && plugStates?.[printer.id] === undefined && <p className="px-3 text-xs text-[var(--brand-text-muted)]">State unknown</p>}
      {plugOutcome && <p role="status" className="px-3 py-1 text-xs text-[var(--brand-text-secondary)]">{plugOutcome}</p>}
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
              {syncing ? '\u27F3 Syncing...' : '\u21BB Sync AMS'}
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
          {printer.filament_slots?.map((slot: any, idx: number) => {
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
                {printer.lights_on != null && mayControl(printer) && (
                  <button
                    onClick={(e) => { e.stopPropagation(); void sendCardCommand('lights') }}
                    disabled={commandPending}
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
      {isOnline(printer) && isActivePrintState(printer.gcode_state) && (
        <div className="px-3 md:px-4 py-2 border-t border-[var(--brand-border)]">
          <PrintProgress percent={printer.mc_percent} remainingMinutes={printer.mc_remaining_time} />
        </div>
      )}
      {/* Bambu speed control — active prints only */}
      {printer.api_type === 'bambu' && isOnline(printer) && isActivePrintState(printer.gcode_state) && (
        <div className="px-3 md:px-4 py-2 border-t border-[var(--brand-border)]">
          <p className="text-xs text-[var(--brand-text-muted)]">
            {reportedPercent == null ? 'Reported speed unknown' : `Last reported speed: ${reportedPercent}%`}
          </p>
          <p className="text-xs text-[var(--brand-text-muted)]">
            {reportedLevel == null ? 'Printer speed mode unknown' : `Last reported mode: ${speedModes[reportedLevel - 1]}`}
          </p>
          {mayControl(printer, true) && <div className="mt-2 space-y-2">
            <p className="text-xs">Selected mode: {speedModes[selectedSpeed - 1]}</p>
            <input type="range" min="1" max="4" step="1" value={selectedSpeed}
              aria-label="Print speed mode" aria-valuetext={speedModes[selectedSpeed - 1]}
              disabled={commandPending} className="w-full min-h-8"
              onChange={event => {
                const level = Number(event.target.value)
                if (!commandBusy.current && mayControl(currentPrinter.current, true) && Number.isInteger(level) && level >= 1 && level <= 4) {
                  setSpeedDraft({ identity: JSON.stringify(getCurrentUser()), printerId: printer.id, level })
                }
              }} />
            <div className="flex justify-between text-xs text-[var(--brand-text-muted)]">
              {speedModes.map(mode => <span key={mode}>{mode}</span>)}
            </div>
            <button disabled={commandPending} className="min-h-11 px-3 py-2 rounded-md text-xs bg-[var(--brand-input-bg)] disabled:opacity-50"
              onClick={event => { event.stopPropagation(); void sendCardCommand('speed', selectedSpeed) }}>Apply speed</button>
          </div>}
        </div>
      )}
      {commandOutcome?.identity === JSON.stringify(getCurrentUser()) && commandOutcome.printerId === printer.id && (
        <div role="status" className="px-3 md:px-4 py-2 text-xs">{commandOutcome.text}</div>
      )}
      {/* Data & Diagnostics toolbar */}
      <div className="px-3 md:px-4 py-2 border-t border-[var(--brand-border)] flex flex-wrap items-center gap-1">
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
      {activePanel === 'telemetry' && <PrinterTelemetryChart printerId={printer.id} apiType={printer.api_type} onClose={() => setActivePanel(null)} />}
      {activePanel === 'nozzle' && <NozzleStatusCard printerId={printer.id} onClose={() => setActivePanel(null)} />}
      {activePanel === 'hms' && <HmsHistoryPanel printerId={printer.id} apiType={printer.api_type} onClose={() => setActivePanel(null)} />}
    </div>
  )
}
