import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import AmsMappingEditor, { useAmsMapping } from './AmsMappingEditor'
import wire from '../../test-fixtures/ams-file-source-wire.json'
afterEach(cleanup)

function Harness({ context = 'file8:printer1', stored = wire.stored, submit = vi.fn() }) {
  const selection = useAmsMapping(wire.preview, context, stored)
  return <><AmsMappingEditor preview={wire.preview} {...selection} />
    <button disabled={!selection.value} onClick={() => submit(selection.value)}>Save</button></>
}

test('restores sparse source four and sends the file ID, skipping unused entries', () => {
  const submit = vi.fn()
  render(<Harness submit={submit} />)
  expect(screen.getByLabelText('Filament 4 AMS slot')).toHaveValue('4')
  expect(screen.queryByLabelText('Filament 1 AMS slot')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Save' }))
  expect(submit).toHaveBeenCalledWith([{ source_slot: 4, target_slot: 4 }])
})

test('context change discards a previous selection and disabling omits the mapping', () => {
  const view = render(<Harness />)
  fireEvent.change(screen.getByLabelText('Filament 4 AMS slot'), { target: { value: '' } })
  expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  view.rerender(<Harness context="file8:printer2" />)
  expect(screen.getByLabelText('Filament 4 AMS slot')).toHaveValue('4')
  fireEvent.click(screen.getByRole('checkbox'))
  expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
})
