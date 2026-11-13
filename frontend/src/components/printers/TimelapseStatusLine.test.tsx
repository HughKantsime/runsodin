import { render, screen, cleanup } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import TimelapseStatusLine, { describeTimelapseStatus } from './TimelapseStatusLine'

const state = vi.hoisted(() => ({ data: undefined as any }))
vi.mock('@tanstack/react-query', () => ({ useQuery: () => ({ data: state.data }) }))
afterEach(() => { cleanup(); state.data = undefined })

const NOW = Date.parse('2026-11-12T10:00:20Z')
const base = { enabled: true, capturing: false, print_job_id: null, started_at: null, frames_captured: 0, last_frame_at: null, last_error: null, will_capture_external_prints: false }

test('off', () => {
  expect(describeTimelapseStatus({ ...base, enabled: false }, NOW)).toBe('Timelapse off')
})
test('recording shows frames and age of the last frame', () => {
  expect(describeTimelapseStatus({ ...base, capturing: true, frames_captured: 412, last_frame_at: '2026-11-12T10:00:00+00:00' }, NOW))
    .toBe('Recording timelapse: 412 frames, last frame 20 s ago')
})
test('singular frame, minutes and no frame yet', () => {
  expect(describeTimelapseStatus({ ...base, capturing: true, frames_captured: 1, last_frame_at: '2026-11-12T09:57:20+00:00' }, NOW))
    .toBe('Recording timelapse: 1 frame, last frame 3 min ago')
  expect(describeTimelapseStatus({ ...base, capturing: true }, NOW)).toBe('Recording timelapse: 0 frames, no frame saved yet')
})
test('recording with a problem appends the plain-words reason', () => {
  expect(describeTimelapseStatus({ ...base, capturing: true, frames_captured: 5, last_frame_at: '2026-11-12T09:50:00+00:00', last_error: 'No new frame has been saved recently.' }, NOW))
    .toBe('Recording timelapse: 5 frames, last frame 10 min ago. No new frame has been saved recently.')
})
test('not recording shows the reason; enabled with no reason waits', () => {
  expect(describeTimelapseStatus({ ...base, last_error: 'The camera is turned off for this printer.' }, NOW))
    .toBe('Not recording: The camera is turned off for this printer.')
  expect(describeTimelapseStatus(base, NOW)).toBe('Timelapse on, waiting for a print')
})
test('anything that is not a status block yields no line', () => {
  expect(describeTimelapseStatus(undefined, NOW)).toBeNull()
  expect(describeTimelapseStatus({ enabled: true }, NOW)).toBeNull()
  expect(describeTimelapseStatus({ enabled: false }, NOW)).toBeNull()
})
test('component renders the line and hides when there is no status', () => {
  state.data = { ...base, capturing: true, frames_captured: 2, last_frame_at: new Date().toISOString() }
  const view = render(<TimelapseStatusLine printerId={9} />)
  expect(screen.getByTestId('timelapse-status')).toHaveTextContent('Recording timelapse: 2 frames, last frame 0 s ago')
  state.data = undefined
  view.rerender(<TimelapseStatusLine printerId={9} />)
  expect(screen.queryByTestId('timelapse-status')).toBeNull()
})
