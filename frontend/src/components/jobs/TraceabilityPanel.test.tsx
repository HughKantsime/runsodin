import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import TraceabilityPanel from './TraceabilityPanel'
import { traceability } from '../../api/traceability'
const access = vi.hoisted(() => ({ licensed: true, allowed: true }))
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ hasFeature: () => access.licensed }) }))
vi.mock('../../permissions', () => ({ canDo: () => access.allowed }))
vi.mock('../../api/traceability', () => ({ traceability: { get: vi.fn(), inspect: vi.fn(), deliver: vi.fn(), certificate: vi.fn(), batch: vi.fn(), policies: vi.fn(), policy: vi.fn() } }))
const record = { job_id: 12, attempt: 0, captured_at: '2026-11-05', inspection_required: true, inspection: { inspection_result: null, inspection_notes: null, inspected_by: null, inspected_at: null }, delivery: { delivered_at: null, delivered_by: null }, file: { name: 'part.gcode', sha256: 'a'.repeat(64) } }
beforeEach(() => { access.licensed = true; access.allowed = true; vi.mocked(traceability.get).mockResolvedValue(record) })
afterEach(() => { cleanup(); vi.clearAllMocks() })
it('hides the feature without entitlement or RBAC permission', () => {
  access.licensed = false
  const { rerender } = render(<TraceabilityPanel />)
  expect(screen.queryByText('Part traceability & certificates')).not.toBeInTheDocument()
  access.licensed = true; access.allowed = false; rerender(<TraceabilityPanel />)
  expect(screen.queryByText('Part traceability & certificates')).not.toBeInTheDocument()
})
it('loads evidence, blocks uninspected delivery, saves inspection and downloads the bundle', async () => {
  render(<TraceabilityPanel />)
  fireEvent.change(screen.getByLabelText('Traceability job ID'), { target: { value: '12' } })
  fireEvent.click(screen.getByText('Load record'))
  await screen.findByText('File: part.gcode')
  expect(screen.getByText('Mark delivered')).toBeDisabled()
  const inspected = { ...record, inspection: { ...record.inspection, inspection_result: 'pass' as const, inspection_notes: 'Measured' } }
  vi.mocked(traceability.inspect).mockResolvedValue(inspected)
  fireEvent.change(screen.getByLabelText('Inspection notes'), { target: { value: 'Measured' } })
  fireEvent.click(screen.getByText('Save inspection'))
  await waitFor(() => expect(traceability.inspect).toHaveBeenCalledWith(12, 'pass', 'Measured'))
  await waitFor(() => expect(screen.getByText('Mark delivered')).not.toBeDisabled())
  fireEvent.click(screen.getByText('Download signed certificate'))
  await waitFor(() => expect(traceability.certificate).toHaveBeenCalledWith(12))
})
it('surfaces server refusals and clears old records on a different job selection', async () => {
  render(<TraceabilityPanel />)
  fireEvent.change(screen.getByLabelText('Traceability job ID'), { target: { value: '12' } })
  fireEvent.click(screen.getByText('Load record'))
  await screen.findByText('File: part.gcode')
  fireEvent.change(screen.getByLabelText('Traceability job ID'), { target: { value: '13' } })
  expect(screen.queryByText('File: part.gcode')).not.toBeInTheDocument()
  vi.mocked(traceability.get).mockRejectedValueOnce(new Error('Job not found'))
  fireEvent.click(screen.getByText('Load record'))
  expect(await screen.findByRole('alert')).toHaveTextContent('Job not found')
})
it('submits a tenant policy and reloads the policy list', async () => {
  vi.mocked(traceability.policies).mockResolvedValue([{ org_id: 1, target_kind: 'printer_tag', target_key: 'QA', required: true }])
  render(<TraceabilityPanel />)
  fireEvent.change(screen.getByLabelText('Organization ID (0 for platform)'), { target: { value: '1' } })
  fireEvent.change(screen.getByLabelText('Tag or product ID'), { target: { value: 'QA' } })
  fireEvent.click(screen.getByText('Save policy'))
  await waitFor(() => expect(traceability.policy).toHaveBeenCalledWith({ org_id: 1, target_kind: 'printer_tag', target_key: 'QA', required: true }))
  expect(await screen.findByText('Printer tag QA: required')).toBeInTheDocument()
})
