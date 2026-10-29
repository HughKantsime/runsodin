import { useEffect, useState } from 'react'
import { config as configApi } from '../../api'

// Treat API strings as untrusted display data. Only DNS origins are useful here;
// never expose IP literals, credentials, proxy lists, or arbitrary error details.
function displayOrigin(value: unknown): string | null {
  if (typeof value !== 'string' || /\s/.test(value)) return null
  try {
    const url = new URL(value)
    const labels = url.hostname.split('.')
    if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash
      || url.pathname !== '/' || labels.length < 2
      || !labels.every(label => /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i.test(label))
      || !/^[a-z][a-z0-9-]*$/i.test(labels[labels.length - 1])) return null
    return url.origin
  } catch {
    return null
  }
}

export default function RemoteAccessStatus() {
  const [url, setUrl] = useState<string | null>(null)
  const [tailnet, setTailnet] = useState(false)
  const [needsIce, setNeedsIce] = useState(false)
  const [check, setCheck] = useState('Loading remote access status…')
  useEffect(() => {
    const controller = new AbortController()
    let stopped = false
    const timer = setTimeout(() => {
      stopped = true
      controller.abort()
      setCheck('Remote access status unavailable.')
    }, 10000)
    configApi.getRemoteAccess(controller.signal).then(data => {
      if (stopped) return
      if (!data || typeof data.configured !== 'boolean') throw new Error('Invalid status')
      if (!data.configured) {
        setCheck('Remote access is not configured.')
        return
      }
      const origin = displayOrigin(data.url)
      setUrl(origin)
      setTailnet(data.tailnet === true)
      setNeedsIce(data.tailnet === true && data.tailnet_ice_configured === false)
      setCheck(!origin ? 'Remote access URL is unavailable. Use an HTTPS hostname.'
        : data.current_origin_verified === true ? 'Connected through the configured remote URL.'
          : 'Remote access is configured. Open the server URL to check connectivity.')
    }).catch(() => {
      if (!stopped) setCheck('Remote access status unavailable.')
    }).finally(() => clearTimeout(timer))
    return () => { stopped = true; controller.abort(); clearTimeout(timer) }
  }, [])
  return <section className="mt-4 border-t border-[var(--brand-card-border)] pt-4" aria-label="Remote access">
    <h3 className="font-semibold">Remote access</h3>
    {url && <p className="text-sm break-all">iOS server URL: <a className="inline-block min-h-6 underline" href={url}>{url}</a></p>}
    {url && tailnet && <p className="text-sm">Use this URL in the ODIN app, not a LAN hostname or IP.</p>}
    {url && needsIce && <p className="text-sm">Live WebRTC video over Tailscale needs ODIN_TAILNET_IP (see the remote access guide). Cameras use the MJPEG/snapshot fallback meanwhile.</p>}
    <p role="status" className="text-sm text-[var(--brand-text-muted)]">{check}</p>
    {check === 'Remote access is not configured.' && <p className="text-xs">Set ODIN_PUBLIC_URL using the remote access guide.</p>}
  </section>
}
