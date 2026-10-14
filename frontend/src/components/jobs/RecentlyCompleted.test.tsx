import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import RecentlyCompleted from './RecentlyCompleted'
import type { RecentTerminalJob } from '../../types'
const image = 'data:image/png;base64,iVBORw0KGgo='
const job: RecentTerminalJob = { id: 1, item_name: 'Owned outcome', status: 'completed', actual_end: '2026-10-12T12:00:00Z', duration_seconds: 0, printer_name: 'Owned printer', model_name: 'Owned model', fail_reason: null, thumbnail_data: image }
afterEach(cleanup)
test('renders authorized thumbnail and zero actual duration', () => {
  render(<RecentlyCompleted jobs={[job]} />)
  expect(screen.getByRole('img', { name: 'Thumbnail for Owned outcome' })).toHaveAttribute('src', image)
  expect(screen.getByText('Owned printer · 0s')).toBeInTheDocument()
  expect(screen.getByText('Owned model')).toBeInTheDocument()
})
test('thumbnail failure becomes placeholder and unknown timing remains explicit', () => {
  render(<RecentlyCompleted jobs={[{ ...job, actual_end: null, duration_seconds: null }]} />)
  fireEvent.error(screen.getByRole('img'))
  expect(screen.queryByRole('img')).not.toBeInTheDocument()
  expect(screen.getByLabelText('Thumbnail unavailable')).toBeInTheDocument()
  expect(screen.getByText('Owned printer · Duration unknown')).toBeInTheDocument()
  expect(screen.getByText('Completion time unknown')).toBeInTheDocument()
})
test('external image never enters img and null related name stays unavailable', () => {
  render(<RecentlyCompleted jobs={[{ ...job, thumbnail_data: 'https://outside.example/private.png', printer_name: null }]} />)
  expect(screen.queryByRole('img')).not.toBeInTheDocument()
  expect(screen.getByText('Printer unavailable · 0s')).toBeInTheDocument()
})
test('loading, error and empty states are independently visible', () => {
  const view = render(<RecentlyCompleted loading />)
  expect(screen.getByRole('status')).toHaveTextContent('Loading recent jobs')
  view.rerender(<RecentlyCompleted error />)
  expect(screen.getByRole('alert')).toHaveTextContent('Recent jobs unavailable')
  view.rerender(<RecentlyCompleted jobs={[]} />)
  expect(screen.getByText('No completed or failed jobs.')).toBeInTheDocument()
})
test('server projection order is preserved and estimates never fill actual duration', () => {
  render(<RecentlyCompleted jobs={[{ ...job, id: 9, item_name: 'First exact receipt', duration_seconds: 7200 }, { ...job, id: 3, item_name: 'Second exact receipt', duration_seconds: null, status: 'failed', fail_reason: 'nozzle_clog' }]} />)
  expect(screen.getAllByText(/exact receipt/).filter(node => node.tagName === 'SPAN').map(node => node.textContent)).toEqual(['First exact receipt', 'Second exact receipt'])
  expect(screen.getByText('Owned printer · 2h')).toBeInTheDocument()
  expect(screen.getByText('Owned printer · Duration unknown')).toBeInTheDocument()
  expect(screen.getByText('nozzle clog')).toBeInTheDocument()
})

test('visible retry invokes its actual supplied refetch action', () => {
  const retry = vi.fn()
  render(<RecentlyCompleted error onRetry={retry} />)
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  expect(retry).toHaveBeenCalledExactlyOnceWith()
})
