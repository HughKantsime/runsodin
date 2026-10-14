import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, test, vi } from 'vitest'
import { refreshPermissions, clearSensitiveBrowserState } from '../../permissions'
import Timelapses from './Timelapses'
const api = vi.hoisted(() => ({ detail: vi.fn() }))
vi.mock('../../api', () => ({ auth: { me: vi.fn() }, printers: { list: async () => [] }, timelapses: { list: async () => ({ timelapses: [{ id: 17, printer_id: 9, status: 'ready', frame_count: 3 }], total: 1 }), detail: api.detail } }))
afterEach(async () => { cleanup(); vi.unstubAllGlobals(); await clearSensitiveBrowserState(); vi.clearAllMocks() })
test('actual permission cache canonical printers.edit enables admin, then current revoked permission blocks action', async () => {
 const user = { id: 3, username: 'Owned admin', role: 'admin', group_id: 1 }
 let granted = true
 vi.stubGlobal('fetch', vi.fn(async url => new Response(JSON.stringify(String(url).endsWith('/auth/me') ? user : { page_access: { cameras: ['admin'] }, action_access: { 'printers.edit': granted ? ['admin', 'operator'] : [] } }))))
 await refreshPermissions()
 const client = new QueryClient({ defaultOptions: { queries: { retry: false } } }); client.setQueryData(['auth', 'me'], user)
 render(<QueryClientProvider client={client}><MemoryRouter><Timelapses /></MemoryRouter></QueryClientProvider>)
 const button = await screen.findByRole('button', { name: 'Delete recording 17' })
 granted = false; await refreshPermissions(); fireEvent.click(button)
 expect(api.detail).not.toHaveBeenCalled()
 client.setQueryData(['auth', 'me'], { ...user, role: 'viewer' })
 await waitFor(() => expect(screen.queryByRole('button', { name: 'Delete recording 17' })).not.toBeInTheDocument())
})
