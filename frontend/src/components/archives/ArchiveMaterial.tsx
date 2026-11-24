import type { Archive } from '../../types/archive'

export default function ArchiveMaterial({ archive }: { archive: Pick<Archive, 'materials' | 'material_source'> }) {
  const materials = archive.materials ?? []
  if (!materials.length) return <span title="Not recorded for this print" aria-label="Material not recorded for this print">—</span>
  return <span>{materials.slice(0, 3).map(m => m.material).join(' + ')}
    {materials.length > 3 && ` +${materials.length - 3}`}
    {(archive.material_source === 'file' || archive.material_source === 'job') && <small> (from {archive.material_source})</small>}
  </span>
}
