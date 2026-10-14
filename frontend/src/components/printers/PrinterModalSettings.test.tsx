import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import PrinterModal from './PrinterModal'
vi.mock('../../permissions', () => ({ canDo: () => true }))
const printer = { id: 199, name: 'synthetic', model: 'P1S', slot_count: 4, api_type: 'bambu', tags: ['existing'], timelapse_enabled: true }
afterEach(cleanup)
function modal(extra = {}) {
 const submit = vi.fn()
 render(<PrinterModal isOpen onClose={vi.fn()} onSubmit={submit} printer={{ ...printer, ...extra }} />)
 return submit
}
it('adds a trimmed unique tag without submitting, removes it and preserves the remaining tags', () => {
 const submit = modal()
 const input = screen.getByPlaceholderText('Type a tag and press Enter')
 for (const value of ['  new  ', 'new']) {
  fireEvent.change(input, { target: { value } }); fireEvent.keyDown(input, { key: 'Enter' })
 }
 expect(submit).not.toHaveBeenCalled()
 expect(screen.getAllByText('new')).toHaveLength(1)
 fireEvent.click(screen.getByText('existing').querySelector('button')!)
 fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }))
 expect(submit).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ tags: ['new'], timelapse_enabled: true }), 199)
})
it('explicitly saves a disabled timelapse setting', () => {
 const submit = modal()
 fireEvent.click(screen.getByRole('checkbox', { name: 'Enable timelapse recording' }))
 fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }))
 expect(submit).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ timelapse_enabled: false }), 199)
})
it('does not clear an unchanged masked camera configuration when editing tags', () => {
 const submit = modal({ camera_url: 'rtsp://synthetic:***@192.0.2.1/live' })
 fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }))
 expect(submit).toHaveBeenCalledOnce()
 expect(submit.mock.calls[0][0]).not.toHaveProperty('camera_url')
})
it.each(['', 'http://192.0.2.2/snapshot'])('saves an explicit camera replacement or removal %s', value => {
 const submit = modal({ camera_url: 'rtsp://synthetic:***@192.0.2.1/live' })
 fireEvent.change(screen.getByPlaceholderText('e.g. http://192.168.1.50:8080/?action=stream'), { target: { value } })
 fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }))
 expect(submit.mock.calls[0][0].camera_url).toBe(value || null)
})
it('blocks changed redacted camera input without echoing it and resets on printer switch', () => {
 const submit = vi.fn()
 const props = { isOpen: true, onClose: vi.fn(), onSubmit: submit }
 const view = render(<PrinterModal {...props} printer={{ ...printer, camera_url: 'rtsp://synthetic:***@192.0.2.1/live' }} />)
 fireEvent.change(screen.getByPlaceholderText('e.g. http://192.168.1.50:8080/?action=stream'), { target: { value: 'rtsp://synthetic:***@192.0.2.2/changed' } })
 fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }))
 expect(submit).not.toHaveBeenCalled()
 expect(screen.getByRole('alert')).toHaveTextContent('Enter a complete replacement camera URL or clear the field.')
 expect(screen.getByRole('alert')).not.toHaveTextContent('192.0.2.2')
 view.rerender(<PrinterModal {...props} printer={{ ...printer, id: 200, camera_url: 'rtsp://synthetic:***@192.0.2.3/live' }} />)
 expect(screen.queryByRole('alert')).not.toBeInTheDocument()
 fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }))
 expect(submit).toHaveBeenCalledOnce()
 expect(submit.mock.calls[0][0]).not.toHaveProperty('camera_url')
 expect(submit.mock.calls[0][1]).toBe(200)
})
