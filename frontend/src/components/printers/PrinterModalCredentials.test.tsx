import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import PrinterModal from './PrinterModal'
const api = vi.hoisted(() => ({ testConnection: vi.fn().mockResolvedValue({ success: true }) }))
vi.mock('../../api', () => ({ printers: api, getPlugConfig: vi.fn().mockResolvedValue(null) }))
vi.mock('../../permissions', () => ({ canDo: () => true }))
afterEach(() => { cleanup(); vi.clearAllMocks() })
const printer = { id: 199, name: 'test', model: 'Prusa', slot_count: 1, api_type: 'prusalink', api_host: '192.168.1.44', has_api_key: true }
it.each(['synthetic-key', 'maker|synthetic-pass'])('includes entered credential in test and save: %s', async credential => {
 const submit = vi.fn()
 render(<PrinterModal isOpen onClose={vi.fn()} onSubmit={submit} printer={printer} />)
 fireEvent.change(screen.getByPlaceholderText('PrusaLink API key or username|password'), { target: { value: credential } })
 fireEvent.click(screen.getByRole('button', { name: 'Test Connection' }))
 await waitFor(() => expect(api.testConnection).toHaveBeenCalledWith(expect.objectContaining({ api_key: credential })))
 fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }))
 expect(submit.mock.calls[0][0].api_key).toBe(credential)
})
it('preserves a saved key when editing without replacement', () => {
 const submit = vi.fn()
 render(<PrinterModal isOpen onClose={vi.fn()} onSubmit={submit} printer={printer} />)
 fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }))
 expect(submit.mock.calls[0][0]).not.toHaveProperty('api_key')
})
it('sends the credential when creating a PrusaLink printer', () => {
 const submit = vi.fn()
 const view = render(<PrinterModal isOpen onClose={vi.fn()} onSubmit={submit} />)
 fireEvent.change(screen.getByRole('option', { name: /PrusaLink/ }).closest('select')!, { target: { value: 'prusalink' } })
 fireEvent.change(screen.getByPlaceholderText('e.g., p1s-01, x1c-floor2'), { target: { value: 'prusa-test' } })
 fireEvent.change(screen.getByPlaceholderText('PrusaLink API key or username|password'), { target: { value: 'synthetic-key' } })
 fireEvent.submit(view.container.querySelector('form')!)
 expect(submit.mock.calls[0][0]).toMatchObject({ api_type: 'prusalink', api_key: 'synthetic-key' })
})
