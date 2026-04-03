// Map page routes to documentation URLs
export const HELP_LINKS: Record<string, { url: string; label: string }> = {
  '/': { url: 'https://docs.runsodin.com/getting-started', label: 'Getting Started Guide' },
  '/printers': { url: 'https://docs.runsodin.com/printers/adding-printers', label: 'Adding Printers' },
  '/printers/detail': { url: 'https://docs.runsodin.com/printers/controls', label: 'Printer Controls' },
  '/jobs': { url: 'https://docs.runsodin.com/jobs/scheduling', label: 'Job Scheduling' },
  '/upload': { url: 'https://docs.runsodin.com/jobs/uploading', label: 'Uploading Files' },
  '/spools': { url: 'https://docs.runsodin.com/inventory/filament', label: 'Filament Tracking' },
  '/cameras': { url: 'https://docs.runsodin.com/cameras/streaming', label: 'Camera Setup' },
  '/orders': { url: 'https://docs.runsodin.com/orders/overview', label: 'Orders & Invoicing' },
  '/analytics': { url: 'https://docs.runsodin.com/reporting/analytics', label: 'Analytics' },
  '/detections': { url: 'https://docs.runsodin.com/vision/vigil-ai', label: 'Vigil AI Setup' },
  '/settings': { url: 'https://docs.runsodin.com/admin/settings', label: 'Admin Settings' },
  '/settings/branding': { url: 'https://docs.runsodin.com/admin/branding', label: 'Custom Branding' },
  '/timeline': { url: 'https://docs.runsodin.com/jobs/timeline', label: 'Timeline View' },
  '/models': { url: 'https://docs.runsodin.com/library/models', label: 'Model Library' },
  '/profiles': { url: 'https://docs.runsodin.com/library/profiles', label: 'Print Profiles' },
  '/products': { url: 'https://docs.runsodin.com/orders/products', label: 'Product Catalog' },
  '/consumables': { url: 'https://docs.runsodin.com/inventory/consumables', label: 'Consumables Tracking' },
  '/maintenance': { url: 'https://docs.runsodin.com/fleet/maintenance', label: 'Maintenance Schedules' },
  '/utilization': { url: 'https://docs.runsodin.com/reporting/utilization', label: 'Utilization Reports' },
  '/alerts': { url: 'https://docs.runsodin.com/fleet/alerts', label: 'Alert Configuration' },
  '/archives': { url: 'https://docs.runsodin.com/fleet/archives', label: 'Print Archives' },
  '/calculator': { url: 'https://docs.runsodin.com/tools/calculator', label: 'Cost Calculator' },
  '/audit': { url: 'https://docs.runsodin.com/admin/audit-log', label: 'Audit Log' },
  '/tv': { url: 'https://docs.runsodin.com/fleet/tv-dashboard', label: 'TV Dashboard Setup' },
}

export function getHelpLink(path: string): { url: string; label: string } | null {
  // Try exact match first, then prefix match
  if (HELP_LINKS[path]) return HELP_LINKS[path]
  const prefix = Object.keys(HELP_LINKS).find(k => path.startsWith(k) && k !== '/')
  return prefix ? HELP_LINKS[prefix] : null
}
