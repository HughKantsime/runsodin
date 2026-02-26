import { useState } from 'react'
import { Search } from 'lucide-react'
import { printers } from '../../api'
import { canDo } from '../../permissions'
import { getShortName } from '../../utils/shared'

export default function FilamentSlotEditor({ slot, allFilaments, spools, printerId, onSave }) {
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
