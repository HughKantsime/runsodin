export const LICENSE_REFRESH_EVENT = 'odin:license-refresh'

export function notifyLicenseRefusal(response: Response, detail: unknown): void {
  if (response.status === 403 && (
    response.headers.has('X-ODIN-License-Feature') ||
    (typeof detail === 'string' && /^Feature '.+' requires /.test(detail))
  )) {
    window.dispatchEvent(new Event(LICENSE_REFRESH_EVENT))
  }
}
