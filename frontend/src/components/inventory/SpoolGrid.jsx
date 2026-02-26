import { canDo } from '../../permissions'
import SpoolCard from './SpoolCard'

export default function SpoolGrid({ spools, printers, sortBy, groupByPrinter, searchQuery, selectedSpools, onToggleSelect, onLoad, onUnload, onUse, onArchive, onEdit, onDry }) {
  if (!spools || spools.length === 0) return null

  let sorted = [...spools]

  if (searchQuery.trim()) {
    const q = searchQuery.toLowerCase()
    sorted = sorted.filter(s =>
      (s.filament_brand || '').toLowerCase().includes(q) ||
      (s.filament_name || '').toLowerCase().includes(q) ||
      (s.filament_material || '').toLowerCase().includes(q)
    )
  }

  if (sortBy === 'printer') {
    sorted.sort((a, b) => {
      if (a.location_printer_id !== b.location_printer_id) return (a.location_printer_id || 999) - (b.location_printer_id || 999)
      return (a.location_slot || 999) - (b.location_slot || 999)
    })
  } else if (sortBy === 'name') {
    sorted.sort((a, b) => `${a.filament_brand} ${a.filament_name}`.localeCompare(`${b.filament_brand} ${b.filament_name}`))
  } else if (sortBy === 'remaining') {
    sorted.sort((a, b) => (a.percent_remaining || 0) - (b.percent_remaining || 0))
  } else if (sortBy === 'material') {
    sorted.sort((a, b) => (a.filament_material || '').localeCompare(b.filament_material || ''))
  }

  function SpoolItem({ spool }) {
    return (
      <div className="relative">
        {canDo('spools.edit') && (
          <input type="checkbox" checked={selectedSpools.has(spool.id)} onChange={() => onToggleSelect(spool.id)} className="absolute top-3 left-3 z-10 rounded border-farm-600" />
        )}
        <div className={canDo('spools.edit') ? 'pl-7' : ''}>
          <SpoolCard spool={spool} onLoad={onLoad} onUnload={onUnload} onUse={onUse} onArchive={onArchive} onEdit={onEdit} onDry={onDry} printers={printers} />
        </div>
      </div>
    )
  }

  if (groupByPrinter && sortBy === 'printer') {
    const groups = {}
    sorted.forEach(s => {
      const key = s.location_printer_id
        ? (printers?.find(p => p.id === s.location_printer_id)?.nickname || printers?.find(p => p.id === s.location_printer_id)?.name || `Printer ${s.location_printer_id}`)
        : 'Unassigned'
      if (!groups[key]) groups[key] = []
      groups[key].push(s)
    })

    return (
      <>
        {Object.entries(groups).map(([group, groupSpools]) => (
          <div key={group} className="mb-4 md:mb-6">
            <h3 className="text-base md:text-lg font-semibold text-farm-200 mb-3">{group}</h3>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3 md:gap-4">
              {groupSpools.map(spool => <SpoolItem key={spool.id} spool={spool} />)}
            </div>
          </div>
        ))}
      </>
    )
  }

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3 md:gap-4">
      {sorted.map(spool => <SpoolItem key={spool.id} spool={spool} />)}
    </div>
  )
}
