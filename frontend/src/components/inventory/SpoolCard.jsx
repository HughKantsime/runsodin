import clsx from 'clsx'
import { Printer, Package, QrCode, Scale, Archive, Pencil, Droplets } from 'lucide-react'
import { canDo } from '../../permissions'
import { ProgressBar, Button, SpoolRing } from '../ui'

const HYGROSCOPIC_TYPES = new Set([
  'PA', 'NYLON_CF', 'NYLON_GF', 'PPS', 'PPS_CF',
  'PETG', 'PETG_CF', 'PC', 'PC_ABS', 'PC_CF', 'TPU', 'PVA',
])

export default function SpoolCard({ spool, onLoad, onUnload, onUse, onArchive, onEdit, onDry, printers }) {
  const percentRemaining = spool.percent_remaining || 0
  const isLow = percentRemaining < 20
  const isEmpty = spool.status === 'empty'
  const isArchived = spool.status === 'archived'

  return (
    <div
      className={clsx(
        "rounded-md p-3 md:p-4 border border-[var(--brand-card-border)] hover:border-[var(--brand-card-border)] transition-colors",
        isArchived && "opacity-50"
      )}
      style={{ background: 'var(--brand-card-bg)', boxShadow: 'var(--brand-card-shadow)' }}
    >
      <div className="flex items-start justify-between mb-3">
        <div className="flex items-center gap-2 md:gap-3 min-w-0">
          <SpoolRing
            color={spool.filament_color_hex ? `#${spool.filament_color_hex}` : '#888'}
            material={spool.filament_material}
            level={percentRemaining}
            empty={isEmpty}
            size={32}
          />
          <div className="min-w-0">
            <h3 className="font-medium text-[var(--brand-text-primary)] text-sm md:text-base truncate">
              {spool.filament_brand} {spool.filament_name}
            </h3>
            <p className="text-xs md:text-sm text-[var(--brand-text-muted)]">{spool.filament_material}</p>
          </div>
        </div>
        <span className={clsx(
          "px-2 py-1 rounded-md text-xs font-medium flex-shrink-0 ml-2",
          spool.status === 'active' ? "bg-green-500/20 text-green-400" :
          spool.status === 'empty' ? "bg-red-500/20 text-red-400" :
          "bg-[var(--brand-input-bg)]/30 text-[var(--brand-text-muted)]"
        )}>
          {spool.status}
        </span>
      </div>

      {/* Weight bar */}
      <div className="mb-3">
        <div className="flex justify-between text-xs md:text-sm mb-1">
          <span className="text-[var(--brand-text-muted)]">Remaining</span>
          <span className="text-[var(--brand-text-secondary)]">{spool.remaining_weight_g?.toFixed(0)}g / {spool.initial_weight_g?.toFixed(0)}g</span>
        </div>
        <ProgressBar
          value={percentRemaining}
          color={isEmpty ? 'red' : isLow ? 'yellow' : 'green'}
          size="md"
        />
      </div>

      {/* Location */}
      <div className="text-xs md:text-sm text-[var(--brand-text-muted)] mb-3">
        {spool.location_printer_id ? (
          <span className="flex items-center gap-1">
            <Printer size={14} />
            {printers?.find(p => p.id === spool.location_printer_id)?.nickname || printers?.find(p => p.id === spool.location_printer_id)?.name || `Printer ${spool.location_printer_id}`}, Slot {spool.location_slot}
          </span>
        ) : spool.storage_location ? (
          <span className="flex items-center gap-1">
            <Package size={14} />
            {spool.storage_location}
          </span>
        ) : (
          <span className="text-[var(--brand-text-muted)]">No location set</span>
        )}
      </div>

      {/* QR Code */}
      <div className="text-xs text-[var(--brand-text-muted)] mb-3 font-mono truncate">
        {spool.qr_code}
      </div>

      {/* Hygroscopic indicator */}
      {HYGROSCOPIC_TYPES.has(spool.filament_material) && (
        <div className="flex items-center gap-1.5 mb-3 text-xs text-amber-400">
          <Droplets size={12} />
          <span>Hygroscopic — dry before use</span>
        </div>
      )}

      {/* Actions */}
      <div className="flex gap-1.5 md:gap-2 flex-wrap">
        {canDo('spools.edit') && spool.location_printer_id ? (
          <Button variant="secondary" size="sm" icon={Package} onClick={() => onUnload(spool)} title="Unload from printer">
            <span className="hidden lg:inline">Unload</span>
          </Button>
        ) : canDo('spools.edit') ? (
          <Button variant="primary" size="sm" icon={Printer} onClick={() => onLoad(spool)} title="Load into printer">
            <span className="hidden lg:inline">Load</span>
          </Button>
        ) : null}
        <a
          href={`/api/spools/${spool.id}/label`}
          target="_blank"
          rel="noopener noreferrer"
          className="px-2 md:px-3 py-1 text-xs rounded-md gap-1.5 bg-[var(--brand-input-bg)] hover:bg-[var(--brand-card-border)] text-[var(--brand-text-secondary)] inline-flex items-center justify-center font-medium transition-colors"
        >
          <QrCode size={14} />
          <span className="hidden lg:inline">Label</span>
        </a>
        {canDo('spools.edit') && <Button variant="secondary" size="sm" icon={Pencil} onClick={() => onEdit(spool)} title="Edit spool">
          <span className="hidden lg:inline">Edit</span>
        </Button>}
        {canDo('spools.edit') && <Button variant="secondary" size="sm" icon={Scale} onClick={() => onUse(spool)} title="Record usage">
          <span className="hidden lg:inline">Use</span>
        </Button>}
        {canDo('spools.edit') && <Button variant="secondary" size="sm" icon={Droplets} onClick={() => onDry(spool)} title="Log drying session" className="hover:bg-amber-900 hover:text-amber-400">
          <span className="hidden lg:inline">Dry</span>
        </Button>}
        {canDo('spools.delete') && spool.status !== 'archived' && (
          <Button variant="secondary" size="sm" icon={Archive} onClick={() => onArchive(spool)} title="Archive" className="hover:bg-red-900 hover:text-red-400">
            <span className="hidden lg:inline">Archive</span>
          </Button>
        )}
      </div>
    </div>
  )
}
