import { useEffect, useRef, useState } from 'react'
import { auth } from '../../api'
import type { User } from '../../types'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { imageBudget, preview, maintenanceAudience, type Preview } from './maintenanceMedia'
export default function BoundedVisionImage({ path, audience, selected = false, alt, className, onClick, onImage }: { path: string; audience: string; selected?: boolean; alt: string; className?: string; onClick?: () => void; onImage?: (value: Preview | null) => void }) {
  const host = useRef<HTMLDivElement | null>(null)
  const [visible, setVisible] = useState(selected || typeof IntersectionObserver === 'undefined')
  useEffect(() => {
    if (selected || typeof IntersectionObserver === 'undefined') { setVisible(true); return }
    const observer = new IntersectionObserver(entries => { setVisible(entries.some(entry => entry.isIntersecting)); if (entries.some(entry => entry.isIntersecting)) activeLease.current?.touch() })
    if (host.current) observer.observe(host.current)
    return () => observer.disconnect()
  }, [selected])
  const queryClient = useQueryClient()
  const { data: principal } = useQuery<User | null>({ queryKey: ['auth', 'me'], queryFn: auth.me, enabled: false })
  const liveAudience = maintenanceAudience(principal)
  const activeLease = useRef<ReturnType<typeof imageBudget.reserve> | null>(null)
  const [image, setImage] = useState<Preview | null>(null), [error, setError] = useState(''), [retry, setRetry] = useState(0)
  const callback = useRef(onImage); callback.current = onImage
  useEffect(() => {
    setImage(null); setError(''); callback.current?.(null)
    if (!visible || !audience || liveAudience !== audience) return
    let live = true; let lease: ReturnType<typeof imageBudget.reserve> | undefined
    try {
      lease = imageBudget.reserve(audience, selected, () => { if (live) { setImage(null); callback.current?.(null); setError('Preview retired to keep image memory bounded.') } })
      activeLease.current = lease
      preview(path, lease, () => maintenanceAudience(queryClient.getQueryData<User | null>(['auth', 'me'])) === audience).then(value => { if (live && !lease!.controller.signal.aborted) { setImage(value); callback.current?.(value) } }).catch(err => { lease!.release(); if (live) setError(err.message || 'Preview unavailable') })
    } catch (err) { setError(err instanceof Error ? err.message : 'Preview unavailable') }
    return () => { live = false; lease?.release(); callback.current?.(null) }
  }, [path, audience, liveAudience, selected, retry, visible])
  return <div ref={host} className={className}>{image && liveAudience === audience ? <img src={image.url} alt={alt} className={className} onClick={() => { activeLease.current?.touch(); onClick?.() }} onError={() => { activeLease.current?.release(); setImage(null); callback.current?.(null); setError('JPEG decode failed. Refresh the preview.') }} /> : <div className={className}><span>{!visible ? 'Preview outside visible area' : error || 'Loading bounded preview...'}</span>{error && <button type="button" onClick={() => setRetry(value => value + 1)}>Retry preview</button>}</div>}</div>
}
