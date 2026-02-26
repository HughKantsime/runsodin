import { ArrowUp, ArrowDown, ArrowUpDown } from 'lucide-react'

function SortIcon({ field, sortField, sortDirection }) {
  if (sortField !== field) return <ArrowUpDown size={12} className="opacity-30" />
  return sortDirection === 'asc' ? <ArrowUp size={12} /> : <ArrowDown size={12} />
}

function SortTh({ label, field, sortField, sortDirection, onSort, className = '' }) {
  return (
    <th scope="col" className={`px-3 md:px-4 py-3 text-left text-[10px] font-mono font-medium text-farm-400 uppercase tracking-wider cursor-pointer hover:text-farm-200 select-none ${className}`} onClick={() => onSort(field)}>
      <div className="flex items-center gap-1">{label} <SortIcon field={field} sortField={sortField} sortDirection={sortDirection} /></div>
    </th>
  )
}

export default function JobTableHeader({ sortField, sortDirection, onSort, allSelected, onSelectAll }) {
  return (
    <thead className="bg-farm-950 border-b border-farm-800">
      <tr>
        <th scope="col" className="px-2 py-3 w-10">
          <input type="checkbox" checked={allSelected} onChange={onSelectAll} className="rounded border-farm-600" aria-label="Select all jobs" />
        </th>
        <SortTh label="Status" field="status" sortField={sortField} sortDirection={sortDirection} onSort={onSort} />
        <SortTh label="Item" field="item_name" sortField={sortField} sortDirection={sortDirection} onSort={onSort} />
        <SortTh label="Pri" field="priority" sortField={sortField} sortDirection={sortDirection} onSort={onSort} />
        <SortTh label="Printer" field="printer" sortField={sortField} sortDirection={sortDirection} onSort={onSort} />
        <th scope="col" className="px-3 md:px-4 py-3 text-left text-[10px] font-mono font-medium text-farm-400 uppercase tracking-wider hidden lg:table-cell">Colors</th>
        <SortTh label="Duration" field="duration_hours" sortField={sortField} sortDirection={sortDirection} onSort={onSort} className="hidden md:table-cell" />
        <SortTh label="Scheduled" field="scheduled_start" sortField={sortField} sortDirection={sortDirection} onSort={onSort} className="hidden lg:table-cell" />
        <th scope="col" className="px-3 md:px-4 py-3 text-left text-[10px] font-mono font-medium text-farm-400 uppercase tracking-wider">Actions</th>
      </tr>
    </thead>
  )
}
