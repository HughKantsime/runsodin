import { cleanup, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, it } from 'vitest'
import Dashboard from './Dashboard'
afterEach(cleanup)
it('associates only running jobs with the matching printer in the actual dashboard page', () => {
 const client = new QueryClient({ defaultOptions: { queries: { enabled: false, retry: false } } })
 const base = { last_seen: new Date().toISOString(), gcode_state: 'RUNNING', filament_slots: [] }
 client.setQueryData(['stats'], { jobs: {} })
 client.setQueryData(['printers'], [{ ...base, id: 199, name: 'Printer Alpha' }, { ...base, id: 200, name: 'Printer Beta' }])
 client.setQueryData(['jobs', 'active'], [])
 client.setQueryData(['print-jobs'], [
  { id: 1, printer_id: 199, status: 'completed', job_name: 'Old completed', progress_percent: 100, started_at: '2026-10-11T10:00:00Z' },
  { id: 2, printer_id: 200, status: 'running', job_name: 'Beta current', progress_percent: 72, remaining_minutes: 125 },
  { id: 3, printer_id: 199, status: 'running', job_name: 'Alpha current', progress_percent: 0, remaining_minutes: 0 },
 ])
 render(<QueryClientProvider client={client}><MemoryRouter><Dashboard /></MemoryRouter></QueryClientProvider>)
 const alpha = screen.getByRole('heading', { name: 'Printer Alpha' }).parentElement!.parentElement!
 const beta = screen.getByRole('heading', { name: 'Printer Beta' }).parentElement!.parentElement!
 expect(within(alpha).getByText('Alpha current')).toBeInTheDocument()
 expect(within(alpha).getByRole('progressbar')).toHaveAttribute('aria-valuenow', '0')
 expect(within(alpha).getByText('0m left')).toBeInTheDocument()
 expect(within(alpha).queryByText('Beta current')).not.toBeInTheDocument()
 expect(within(alpha).queryByText('Old completed')).not.toBeInTheDocument()
 expect(within(beta).getByText('Beta current')).toBeInTheDocument()
 expect(within(beta).getByRole('progressbar')).toHaveAttribute('aria-valuenow', '72')
 expect(within(beta).getByText('2h 5m left')).toBeInTheDocument()
})
