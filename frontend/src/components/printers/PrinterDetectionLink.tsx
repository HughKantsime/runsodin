import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { vision } from '../../api'

/**
 * Printer page: next to the unchanged notice, a link to the detection ODIN recorded when
 * the printer's own camera reported a problem. Detections are listed for platform
 * administrators only, so everyone else gets an empty list and no link.
 */
export default function PrinterDetectionLink({ printer }: { printer: { id: number; api_type?: string | null } }) {
  const bambu = printer.api_type === 'bambu'
  const { data } = useQuery({
    queryKey: ['vision-detections', 'printer-source', printer.id],
    enabled: bambu,
    queryFn: () => vision.getDetections({ printer_id: printer.id, source: 'printer', limit: 1 }),
    refetchInterval: 60000,
    retry: false,
  })
  const latest = bambu ? data?.items?.[0] : undefined
  // Newest first: once it has been reviewed there is nothing left to point at.
  if (!latest || latest.source !== 'printer' || !['pending', 'informational'].includes(latest.status)) return null
  return <p className="my-2 text-sm">
    <Link className="underline" to={`/detections?source=printer&printer=${printer.id}`}>
      The printer&apos;s camera reported a problem: view the detection
    </Link>
  </p>
}
