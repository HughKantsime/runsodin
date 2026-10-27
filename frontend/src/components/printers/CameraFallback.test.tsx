import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import CameraFallback from './CameraFallback'
afterEach(() => vi.useRealTimers())
test('fallback uses same-origin snapshots, refreshes, and stops when disabled', () => {
  vi.useFakeTimers()
  const { rerender, unmount } = render(<CameraFallback cameraId={9} active />)
  const first = screen.getByRole('img').getAttribute('src')
  expect(first).toMatch(/^\/api\/cameras\/9\/snapshot\?t=/)
  expect(screen.getByRole('status')).toHaveTextContent('Showing snapshots every 5 seconds')
  act(() => vi.advanceTimersByTime(5000))
  expect(screen.getByRole('img').getAttribute('src')).not.toBe(first)
  fireEvent.error(screen.getByRole('img'))
  expect(screen.getByRole('status')).toHaveTextContent('Camera snapshots unavailable')
  fireEvent.load(screen.getByRole('img'))
  expect(screen.getByRole('status')).toHaveTextContent('Showing snapshots')
  rerender(<CameraFallback cameraId={9} active={false} />)
  expect(screen.queryByRole('img')).toBeNull()
  expect(vi.getTimerCount()).toBe(0)
  unmount()
})
