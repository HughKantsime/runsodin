import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { FileFactChips, PrinterFitLine, fileFactLabels } from './FileFacts'
import { ModelCard } from './ModelLibraryFields'

vi.mock('@tanstack/react-query', () => ({ useQueryClient: () => ({ invalidateQueries: vi.fn() }) }))
vi.mock('../../permissions', () => ({ canDo: (action: string) => action !== 'models.plates' }))
afterEach(cleanup)

const fit = { count: 2, printers: [{ id: 20, name: 'X1C-1' }, { id: 25, name: 'X1C-2' }], sliced_for: 'Bambu Lab X1 Carbon' }
const facts = { nozzle_diameter: 0.4, layer_height: 0.2, plate_type: 'textured_plate', supports: false, filament_colors: ['#00AE42', '#000000', '#000000'] }
const noFacts = { nozzle_diameter: null, layer_height: null, plate_type: null, supports: null, filament_colors: [] }
const card = (model: Record<string, unknown>) => render(
  <ModelCard model={{ id: 9, name: 'Bracket', ...model }} onEdit={vi.fn()} onDelete={vi.fn()} onSchedule={vi.fn()}
    onToggleFavorite={vi.fn()} onView3D={vi.fn()} onRevisions={vi.fn()} />)

test('the line gives the count and lists the printer names on click', () => {
  render(<PrinterFitLine fit={fit} />)
  const line = screen.getByRole('button', { name: 'Runs on 2 of your printers' })
  expect(line).toHaveAttribute('aria-expanded', 'false')
  expect(screen.queryByRole('list')).not.toBeInTheDocument()
  fireEvent.click(line)
  expect(line).toHaveAttribute('aria-expanded', 'true')
  const names = within(screen.getByRole('list', { name: 'Printers this file runs on' })).getAllByRole('listitem')
  expect(names.map(item => item.textContent)).toEqual(['X1C-1', 'X1C-2'])
  fireEvent.click(line)
  expect(screen.queryByRole('list')).not.toBeInTheDocument()
})

test('no fitting printer names the printer the file was sliced for', () => {
  const { container, rerender } = render(<PrinterFitLine fit={{ count: 0, printers: [], sliced_for: 'Bambu Lab P2S' }} />)
  expect(container).toHaveTextContent('Runs on none of your printers. It was sliced for Bambu Lab P2S.')
  expect(screen.queryByRole('button')).not.toBeInTheDocument()
  rerender(<PrinterFitLine fit={{ count: 0, printers: [], sliced_for: null }} />)
  expect(container.textContent).toBe('Runs on none of your printers.')
})

test('no answer from the server renders no line', () => {
  const { container } = render(<><PrinterFitLine fit={null} /><PrinterFitLine /></>)
  expect(container).toBeEmptyDOMElement()
})

test('file facts are listed as the file states them, colours by name', () => {
  render(<FileFactChips facts={facts} />)
  const chips = within(screen.getByRole('list', { name: 'File details' })).getAllByRole('listitem').map(item => item.textContent)
  expect(chips).toEqual(['0.4 mm nozzle', '0.2 mm layers', 'Textured plate', 'Supports off', 'Dark Green', 'Black'])
  expect(fileFactLabels({ ...noFacts, supports: true })).toEqual(['Supports on'])
})

test('missing facts render nothing', () => {
  const { container } = render(<><FileFactChips facts={noFacts} /><FileFactChips facts={null} /><FileFactChips /></>)
  expect(container).toBeEmptyDOMElement()
  expect(fileFactLabels({ ...noFacts, layer_height: 0, plate_type: '  ', filament_colors: ['not a colour'] })).toEqual([])
  // One stated fact is shown alone; the others are not filled in.
  expect(fileFactLabels({ ...noFacts, nozzle_diameter: 0.6 })).toEqual(['0.6 mm nozzle'])
})

test('the model card shows the line and the facts', () => {
  card({ printer_fit: fit, file_facts: facts })
  fireEvent.click(screen.getByRole('button', { name: 'Runs on 2 of your printers' }))
  expect(screen.getByText('X1C-2')).toBeInTheDocument()
  expect(screen.getByRole('list', { name: 'File details' })).toHaveTextContent('0.4 mm nozzle')
})

test('a card without them is unchanged, and the additions wrap at phone width', () => {
  const plain = card({}).container.innerHTML
  cleanup()
  expect(card({ printer_fit: null, file_facts: noFacts }).container.innerHTML).toBe(plain)
  cleanup()
  const { container } = card({ printer_fit: fit, file_facts: facts })
  // The card keeps its own box and phone paddings; nothing added sets a width.
  expect(container.firstElementChild).toHaveClass('overflow-hidden')
  expect(container.querySelector('.p-3.md\\:p-4')).not.toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Runs on 2 of your printers' }))
  const lists = screen.getAllByRole('list')
  expect(lists).toHaveLength(2)
  for (const list of lists) expect(list).toHaveClass('flex', 'flex-wrap')
  expect(screen.getByRole('button', { name: 'Runs on 2 of your printers' })).toHaveClass('max-w-full')
  const added = [...lists, ...lists.flatMap(list => [...list.children]), screen.getByRole('button', { name: 'Runs on 2 of your printers' })]
  for (const element of added) expect(element.className).not.toMatch(/(^|\s)(w-\[|min-w-\[|w-\d|whitespace-nowrap)/)
})
