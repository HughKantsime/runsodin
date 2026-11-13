import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { applyTheme } from '../../hooks/themePreferences'
import PrinterDetectionLink from './PrinterDetectionLink'

const api = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('../../api', () => ({ vision: { getDetections: api.get } }))
afterEach(() => { cleanup(); vi.clearAllMocks(); localStorage.clear(); applyTheme('dark', 'classic') })

function mount(printer: { id: number; api_type?: string }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><MemoryRouter><PrinterDetectionLink printer={printer} /></MemoryRouter></QueryClientProvider>)
}
const detection = (status: string, source = 'printer') => ({ items: [{ id: 5, printer_id: 9, status, source, detection_type: 'spaghetti' }], total: 1 })

describe('printer page link to a printer-camera detection', () => {
  it.each(['classic', 'foundry'] as const)('%s: links an unreviewed detection to the filtered Detections page', async family => {
    localStorage.setItem('odin-theme-family', family)
    applyTheme('dark', family)
    api.get.mockResolvedValue(detection('informational'))
    mount({ id: 9, api_type: 'bambu' })
    const link = await screen.findByRole('link', { name: "The printer's camera reported a problem: view the detection" })
    expect(link).toHaveAttribute('href', '/detections?source=printer&printer=9')
    expect(api.get).toHaveBeenCalledWith({ printer_id: 9, source: 'printer', limit: 1 })
  })
  it.each([['reviewed', detection('dismissed')], ['none (or not a platform administrator)', { items: [], total: 0 }], ['a Vigil row from an older server', detection('pending', 'vigil')]])('shows nothing for %s', async (_, response) => {
    api.get.mockResolvedValue(response)
    mount({ id: 9, api_type: 'bambu' })
    await waitFor(() => expect(api.get).toHaveBeenCalled())
    await Promise.resolve()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })
  it('asks nothing for a printer that is not a Bambu', () => {
    mount({ id: 4, api_type: 'moonraker' })
    expect(api.get).not.toHaveBeenCalled()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })
  it('a failed request leaves the page as it was', async () => {
    api.get.mockRejectedValue(new Error('offline'))
    mount({ id: 9, api_type: 'bambu' })
    await waitFor(() => expect(api.get).toHaveBeenCalled())
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })
})
