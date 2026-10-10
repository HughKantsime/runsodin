import React from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import BackupRestore from './BackupRestore'
afterEach(() => { cleanup(); vi.unstubAllGlobals() })
function selectBackup() {
  const { container } = render(<BackupRestore />)
  const input = container.querySelector('input')!
  expect(input.accept).toBe('.db,.dump')
  fireEvent.change(input, { target: { files: [new File(['fixture'], 'backup.dump')] } })
  fireEvent.click(screen.getByRole('button', { name: 'Stage Restore' }))
}
it('describes a staged restore and separate full-volume backup', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ status: 'staged', restart_required: true, pre_restore_backup: 'before.db' }) }))
  selectBackup()
  expect(screen.getByText(/installation encryption keys require a separate/)).toBeInTheDocument()
  expect(await screen.findByRole('status')).toHaveTextContent('Restore validated and staged')
  expect(screen.getByRole('status')).toHaveTextContent('before.db')
  expect(screen.queryByText(/Database restored successfully/)).not.toBeInTheDocument()
})
it('rejects unexpected success instead of claiming restoration', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ status: 'success' }) }))
  selectBackup()
  expect(await screen.findByRole('alert')).toHaveTextContent('may already be staged')
  expect(screen.queryByRole('status')).not.toBeInTheDocument()
})

it('warns of uncertainty when the response is lost', async () => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('connection lost')))
  selectBackup()
  expect(await screen.findByRole('alert')).toHaveTextContent('may already be staged')
})
it('shows definite server rejection without claiming an applied restore', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, json: async () => ({ detail: 'Backup format does not match this database' }) }))
  selectBackup()
  expect(await screen.findByRole('alert')).toHaveTextContent('Backup format does not match')
})
