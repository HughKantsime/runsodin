import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import RecoveryCodes from './RecoveryCodes'

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

it('only exports when asked and releases the text download after use', async () => {
  const codes = ['example-first', 'example-second']
  const copy = vi.fn().mockResolvedValue(undefined)
  vi.stubGlobal('navigator', Object.assign(Object.create(navigator), { clipboard: { writeText: copy } }))
  const makeUrl = vi.fn((_file: Blob) => 'blob:example')
  const revoke = vi.fn()
  vi.stubGlobal('URL', Object.assign(class extends URL {}, {createObjectURL: makeUrl, revokeObjectURL: revoke}))
  const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function () {
    expect(this.download).toBe('odin-recovery-codes.txt')
    expect(this.href).toBe('blob:example')
  })
  const storage = vi.spyOn(Storage.prototype, 'setItem')
  const saved = vi.fn()
  render(<RecoveryCodes codes={codes} onSaved={saved} />)
  expect(screen.getByRole('dialog')).toBeVisible()
  expect(copy).not.toHaveBeenCalled()
  expect(makeUrl).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', {name: 'Copy'}))
  expect(await screen.findByRole('status')).toHaveTextContent('Recovery codes copied.')
  expect(copy).toHaveBeenCalledWith(codes.join('\n'))
  fireEvent.click(screen.getByRole('button', {name: 'Download (.txt)'}))
  expect(download).toHaveBeenCalledOnce()
  const file: Blob = makeUrl.mock.lastCall![0]
  const content = await new Promise(resolve => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result)
    reader.readAsText(file)
  })
  expect(content).toBe(codes.join('\n') + '\n')
  await waitFor(() => expect(revoke).toHaveBeenCalledWith('blob:example'))
  expect(storage).not.toHaveBeenCalled()
  expect(saved).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', {name: 'I saved these'}))
  expect(saved).toHaveBeenCalledOnce()
})

it('keeps the codes visible through repeated cancel and a native close', () => {
  const saved = vi.fn()
  render(<RecoveryCodes codes={['example-code']} onSaved={saved} />)
  const dialog = screen.getByRole('dialog') as HTMLDialogElement
  const showModal = vi.fn(() => { dialog.open = true })
  Object.defineProperty(dialog, 'showModal', {value: showModal})
  for (let i = 0; i < 2; i++) {
    expect(fireEvent(dialog, new Event('cancel', {cancelable: true}))).toBe(false)
  }
  // The browser removes open before notifying React about a close.
  dialog.removeAttribute('open')
  fireEvent(dialog, new Event('close'))
  expect(showModal).toHaveBeenCalledOnce()
  expect(screen.getByRole('dialog')).toBeVisible()
  expect(screen.getByText('example-code')).toBeVisible()
  const acknowledge = screen.getByRole('button', {name: 'I saved these'})
  expect(acknowledge).toBeVisible()
  expect(acknowledge).toHaveClass('min-h-11')
  expect(saved).not.toHaveBeenCalled()
})
