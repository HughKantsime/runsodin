import clsx from 'clsx'
import { Printer, Package, QrCode, Scale, Archive, Pencil, Droplets } from 'lucide-react'
import { canDo } from '../../permissions'

const HYGROSCOPIC_TYPES = new Set([
  'PA', 'NYLON_CF', 'NYLON_GF', 'PPS', 'PPS_CF',
  'PETG', 'PETG_CF', 'PC', 'PC_ABS', 'PC_CF', 'TPU', 'PVA',
])

export default function SpoolCard({ spool, onLoad, onUnload, onUse, onArchive, onEdit, onDry, printers }) {
  const percentRemaining = spool.percent_remaining || 0
  const isLow = percentRemaining < 20
  const isEmpty = spool.status === 'empty'
  const isArchived = spool.status === 'archived'

  const statusColor = isEmpty ? 'bg-red-500' : isLow ? 'bg-yellow-500' : 'bg-green-500'

  return (
    <div className={clsx(
      "bg-farm-900 rounded-lg p-3 md:p-4 border border-farm-800 hover:border-farm-700 transition-colors",
      isArchived && "opacity-50"
    )}>
      <div className="flex items-start justify-between mb-3">
        <div className="flex items-center gap-2 md:gap-3 min-w-0">
          {spool.filament_color_hex && (
            <div
              className="w-7 h-7 md:w-8 md:h-8 rounded-full border-2 border-farm-700 flex-shrink-0"
              style={{ backgroundColor: `#${spool.filament_color_hex}` }}
            />
          )}
          <div className="min-w-0">
            <h3 className="font-medium text-farm-100 text-sm md:text-base truncate">
              {spool.filament_brand} {spool.filament_name}
            </h3>
            <p className="text-xs md:text-sm text-farm-400">{spool.filament_material}</p>
          </div>
        </div>
        <span className={clsx(
          "px-2 py-1 rounded-lg text-xs font-medium flex-shrink-0 ml-2",
          spool.status === 'active' ? "bg-green-500/20 text-green-400" :
          spool.status === 'empty' ? "bg-red-500/20 text-red-400" :
          "bg-farm-700/30 text-farm-400"
        )}>
          {spool.status}
        </span>
      </div>

      {/* Weight bar */}
      <div className="mb-3">
        <div className="flex justify-between text-xs md:text-sm mb-1">
          <span className="text-farm-400">Remaining</span>
          <span className="text-farm-200">{spool.remaining_weight_g?.toFixed(0)}g / {spool.initial_weight_g?.toFixed(0)}g</span>
        </div>
        <div className="h-2 bg-farm-800 rounded-full overflow-hidden">
          <div
            className={clsx("h-full transition-all", statusColor)}
            style={{ width: `${percentRemaining}%` }}
          />
        </div>
      </div>

      {/* Location */}
      <div className="text-xs md:text-sm text-farm-400 mb-3">
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
          <span className="text-farm-500">No location set</span>
        )}
      </div>

      {/* QR Code */}
      <div className="text-xs text-farm-500 mb-3 font-mono truncate">
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
          <button
            onClick={() => onUnload(spool)}
            className="px-2 md:px-3 py-1.5 bg-farm-800 hover:bg-farm-700 rounded-lg text-xs md:text-sm text-farm-200 flex items-center justify-center gap-1"
            title="Unload from printer"
          >
            <Package size={14} />
            <span className="hidden lg:inline">Unload</span>
          </button>
        ) : canDo('spools.edit') ? (
          <button
            onClick={() => onLoad(spool)}
            className="px-2 md:px-3 py-1.5 bg-print-600 hover:bg-print-500 rounded-lg text-xs md:text-sm text-white flex items-center justify-center gap-1"
            title="Load into printer"
          >
            <Printer size={14} />
            <span className="hidden lg:inline">Load</span>
          </button>
        ) : null}
        <a
          href={`/api/spools/${spool.id}/label`}
          target="_blank"
          rel="noopener noreferrer"
          className="px-2 md:px-3 py-1.5 bg-farm-800 hover:bg-farm-700 rounded-lg text-xs md:text-sm text-farm-200 flex items-center justify-center gap-1"
        >
          <QrCode size={14} />
          <span className="hidden lg:inline">Label</span>
        </a>
        {canDo('spools.edit') && <button
          onClick={() => onEdit(spool)}
          className="px-2 md:px-3 py-1.5 bg-farm-800 hover:bg-farm-700 rounded-lg text-xs md:text-sm text-farm-200 flex items-center justify-center gap-1"
          title="Edit spool"
        >
          <Pencil size={14} />
          <span className="hidden lg:inline">Edit</span>
        </button>}
        {canDo('spools.edit') && <button
          onClick={() => onUse(spool)}
          className="px-2 md:px-3 py-1.5 bg-farm-800 hover:bg-farm-700 rounded-lg text-xs md:text-sm text-farm-200 flex items-center justify-center gap-1"
          title="Record usage"
        >
          <Scale size={14} />
          <span className="hidden lg:inline">Use</span>
        </button>}
        {canDo('spools.edit') && <button
          onClick={() => onDry(spool)}
          className="px-2 md:px-3 py-1.5 bg-farm-800 hover:bg-amber-900 rounded-lg text-xs md:text-sm text-farm-200 hover:text-amber-400 flex items-center justify-center gap-1"
          title="Log drying session"
        >
          <Droplets size={14} />
          <span className="hidden lg:inline">Dry</span>
        </button>}
        {canDo('spools.delete') && spool.status !== 'archived' && (
          <button
            onClick={() => onArchive(spool)}
            className="px-2 md:px-3 py-1.5 bg-farm-800 hover:bg-red-900 rounded-lg text-xs md:text-sm text-farm-200 hover:text-red-400 flex items-center justify-center gap-1"
            title="Archive"
          >
            <Archive size={14} />
            <span className="hidden lg:inline">Archive</span>
          </button>
        )}
      </div>
    </div>
  )
}
