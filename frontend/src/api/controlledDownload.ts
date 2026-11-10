/** Poll background backup preparation using the same authorized request. */
async function preparedDownload(url: string, init: RequestInit): Promise<Response> {
  const deadline = Date.now() + 15 * 60 * 1000
  for (;;) {
    const response = await fetch(url, init)
    if (response.status !== 202) return response
    if (Date.now() >= deadline) throw new Error('Backup preparation timed out; try again')
    await new Promise(resolve => setTimeout(resolve, 2000))
  }
}

/** Privileged downloads require a real acknowledgement when marks exist. */
export async function controlledDownload(url: string, kind: 'backup' | 'support bundle' | 'signed audit export'): Promise<Response> {
  const response = await preparedDownload(url, { credentials: 'include' })
  if (response.status !== 403) return response
  const error = await response.clone().json().catch(() => null)
  if (error?.detail?.code !== 'controlled_part_acknowledgement_required') return response
  const warning = kind === 'signed audit export'
    ? 'This signed audit export contains unredacted controlled details outside your audience. I acknowledge responsibility for protecting the downloaded copy.'
    : kind === 'backup'
    ? 'This backup contains controlled work, including work outside your audience. I acknowledge responsibility for protecting the downloaded copy.'
    : 'This support bundle excludes controlled rows and logs. I acknowledge this audited administrative download.'
  if (!window.confirm(warning)) throw new Error('Download cancelled')
  const [path, query] = url.split('?')
  const acknowledgeURL = `${path}/acknowledge${query ? `?${query}` : ''}`
  return preparedDownload(acknowledgeURL, { method: 'POST', credentials: 'include', headers: { 'X-ODIN-Controlled-Ack': 'true' } })
}

/** Stage only after the server identifies controlled data and the admin acknowledges it. */
export async function controlledRestore(url: string, init: RequestInit): Promise<Response> {
  const response = await fetch(url, init)
  if (response.status !== 403) return response
  const error = await response.clone().json().catch(() => null)
  if (error?.detail?.code !== 'controlled_part_acknowledgement_required') return response
  if (!window.confirm('This restore replaces controlled work and audience permissions. I acknowledge responsibility for the restored data and access.')) {
    throw new Error('Restore cancelled')
  }
  const headers = new Headers(init.headers)
  headers.set('X-ODIN-Controlled-Ack', 'true')
  return fetch(`${url}?acknowledge_controlled=true`, { ...init, method: 'POST', headers })
}
