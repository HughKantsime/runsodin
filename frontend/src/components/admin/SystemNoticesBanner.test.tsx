import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import dashboardSource from '../../pages/dashboard/Dashboard.tsx?raw'
import settingsSource from '../../pages/admin/Settings.tsx?raw'
import SystemNoticesBanner from './SystemNoticesBanner'

// Wire shape of GET /system/notices (backend/modules/system/routes_backup.py system_notices).
const NOTICE = {
  kind: 'restore_quarantined', severity: 'critical', title: 'Database restore was not applied',
  message: 'Archive schema differs The current database was not changed. Re-stage or delete the archive in Settings > System > Backups.',
  archive: 'failed_restore_20261015_101500_000001.dump', failed_at: '2026-10-15T10:15:00+00:00', link: '/settings?tab=system#backups',
}
const api = vi.hoisted(() => ({ list: vi.fn() }))
const org = vi.hoisted(() => ({ value: { isAdmin: true } }))
vi.mock('../../api/system', () => ({ systemNotices: { list: api.list } }))
vi.mock('../../contexts/OrgContext', () => ({ useOrg: () => org.value }))
beforeEach(() => { org.value = { isAdmin: true }; api.list.mockResolvedValue({ notices: [NOTICE] }) })
afterEach(() => { cleanup(); vi.clearAllMocks() })

function banner() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><MemoryRouter><SystemNoticesBanner /></MemoryRouter></QueryClientProvider>)
}

test('superadmins see the quarantine notice with a link to Settings > System > Backups', async () => {
  banner()
  const alert = await screen.findByRole('alert')
  expect(alert).toHaveTextContent('Database restore was not applied')
  expect(alert).toHaveTextContent('Archive schema differs')
  expect(screen.getByRole('link', { name: 'Open backups' })).toHaveAttribute('href', '/settings?tab=system#backups')
})

test('nothing renders when there is no active notice', async () => {
  api.list.mockResolvedValue({ notices: [] })
  banner()
  await waitFor(() => expect(api.list).toHaveBeenCalled())
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})

test('non-superadmins neither see nor request system notices', async () => {
  org.value = { isAdmin: false }
  banner()
  await new Promise((resolve) => setTimeout(resolve, 20))
  expect(api.list).not.toHaveBeenCalled()
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})

test('the dashboard renders the banner and Settings honours the tab deep link', () => {
  const dashboard = dashboardSource
  expect(dashboard).toContain("import SystemNoticesBanner from '../../components/admin/SystemNoticesBanner'")
  expect(dashboard).toContain('<SystemNoticesBanner />')
  const settings = settingsSource
  expect(settings).toContain("searchParams.get('tab')")
})
