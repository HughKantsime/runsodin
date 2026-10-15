// Map page routes to documentation URLs. Every target must be a page (and, when given,
// a heading anchor) of the Docusaurus site in odin-site/docs — checked by
// tests/test_contracts/test_help_links_docs_targets.py against the docs source tree.
export const HELP_LINKS: Record<string, { url: string; label: string }> = {
  '/': { url: 'https://docs.runsodin.com/getting-started/quick-start', label: 'Quick Start Guide' },
  '/printers': { url: 'https://docs.runsodin.com/configuration/printers', label: 'Adding Printers' },
  '/printers/detail': { url: 'https://docs.runsodin.com/features/mid-print-controls', label: 'Printer Controls' },
  '/jobs': { url: 'https://docs.runsodin.com/features/scheduler', label: 'Job Scheduling' },
  '/upload': { url: 'https://docs.runsodin.com/features/model-library#uploading-models', label: 'Uploading Files' },
  '/spools': { url: 'https://docs.runsodin.com/features/filament-inventory', label: 'Filament Tracking' },
  '/cameras': { url: 'https://docs.runsodin.com/features/cameras', label: 'Camera Setup' },
  '/orders': { url: 'https://docs.runsodin.com/features/orders', label: 'Orders & Invoicing' },
  '/analytics': { url: 'https://docs.runsodin.com/features/analytics', label: 'Analytics' },
  '/detections': { url: 'https://docs.runsodin.com/configuration/ai-detection', label: 'Vigil AI Setup' },
  '/settings': { url: 'https://docs.runsodin.com/configuration/users-and-rbac', label: 'Users & Access Control' },
  '/settings/branding': { url: 'https://docs.runsodin.com/configuration/branding', label: 'Custom Branding' },
  '/timeline': { url: 'https://docs.runsodin.com/features/scheduler', label: 'Job Scheduler' },
  '/models': { url: 'https://docs.runsodin.com/features/model-library', label: 'Model Library' },
  '/profiles': { url: 'https://docs.runsodin.com/features/klipper-profiles', label: 'Slicer & Printer Profiles' },
  '/products': { url: 'https://docs.runsodin.com/features/orders#products--bill-of-materials', label: 'Product Catalog' },
  '/consumables': { url: 'https://docs.runsodin.com/features/orders#consumables-inventory', label: 'Consumables Tracking' },
  '/maintenance': { url: 'https://docs.runsodin.com/features/maintenance', label: 'Maintenance Schedules' },
  '/utilization': { url: 'https://docs.runsodin.com/features/analytics#printer-utilization-report', label: 'Utilization Reports' },
  '/alerts': { url: 'https://docs.runsodin.com/configuration/smtp-and-alerts', label: 'Alert Configuration' },
  '/archives': { url: 'https://docs.runsodin.com/features/print-archive', label: 'Print Archives' },
  '/calculator': { url: 'https://docs.runsodin.com/features/analytics#cost-calculator', label: 'Cost Calculator' },
  '/audit': { url: 'https://docs.runsodin.com/security', label: 'Security & Audit Log' },
  '/tv': { url: 'https://docs.runsodin.com/features/tv-dashboard', label: 'TV Dashboard Setup' },
}

export function getHelpLink(path: string): { url: string; label: string } | null {
  // Try exact match first, then prefix match
  if (HELP_LINKS[path]) return HELP_LINKS[path]
  const prefix = Object.keys(HELP_LINKS).find(k => path.startsWith(k) && k !== '/')
  return prefix ? HELP_LINKS[prefix] : null
}
