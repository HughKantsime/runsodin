import { useEffect, useState } from 'react'
import { config as configApi } from '../../api'
import type { RemoteAccess } from '../../api/system'

export default function RemoteAccessStatus() {
  const [status, setStatus] = useState<RemoteAccess | null>(null)
  const [check, setCheck] = useState('Loading remote access status…')
  useEffect(() => {
    const controller = new AbortController()
    let stopped = false
    const timer = setTimeout(() => controller.abort(), 10000)
    configApi.getRemoteAccess().then(async data => {
      if (stopped) return
      setStatus(data)
      if (!data.check_url) { setCheck('Remote access is not configured.'); return }
      setCheck('Checking the remote URL from this browser…')
      try {
        const response = await fetch(data.check_url, { signal: controller.signal, credentials: 'omit', redirect: 'error', cache: 'no-store' })
        const body = await response.json()
        if (!response.ok || body.status !== 'ok' || typeof body.version !== 'string') throw new Error()
        if (!stopped) setCheck('ODIN answers at this HTTPS URL.')
      } catch {
        if (!stopped) setCheck('Could not verify from this browser. Check Tailscale, DNS, HTTPS and CORS.')
      }
    }).catch(() => { if (!stopped) setCheck('Remote access status unavailable.') })
      .finally(() => clearTimeout(timer))
    return () => { stopped = true; controller.abort(); clearTimeout(timer) }
  }, [])
  return <section className="mt-4 border-t border-[var(--brand-card-border)] pt-4" aria-label="Remote access">
    <h3 className="font-semibold">Remote access</h3>
    {status?.url && <p className="text-sm break-all">iOS server URL: <a className="underline" href={status.url}>{status.url}</a></p>}
    <p role="status" className="text-sm text-[var(--brand-text-muted)]">{check}</p>
    {!status?.configured && <p className="text-xs">Set ODIN_PUBLIC_URL using the remote access guide.</p>}
  </section>
}
