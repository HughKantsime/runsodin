// Mobile web pass, slice 4: source census. Heavy modals (printer, QR, dispatch, education, jobs, spools) are not
// rendered here; this pins the marker to the intended single action-row wrapper and CameraModal's ownHeader.
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { expect, test } from 'vitest'

const src = (rel: string) => readFileSync(resolve(process.cwd(), 'src', rel), 'utf8')
const markers = (text: string) => (text.match(/<div data-modal-footer className="[^"]+">/g) || []).length
const anyMarker = (text: string) => (text.match(/data-modal-footer/g) || []).length

const EXPECTED: Record<string, number> = {
  'components/orders/OrderModals.tsx': 3,
  'pages/models/Models.tsx': 2,
  'components/jobs/FailureReasonModal.tsx': 1,
  'components/jobs/JobModals.tsx': 3,
  'components/inventory/SpoolModals.tsx': 4,
  'components/inventory/SpoolEditModals.tsx': 2,
  'components/education/CostCenterManager.tsx': 1,
  'components/education/SubmissionUploadModal.tsx': 1,
  'components/printers/DispatchRecoveryPanel.tsx': 1,
  'components/printers/PrinterModal.tsx': 1,
  'components/shared/UpgradeModal.tsx': 1,
  'components/inventory/QRScannerModal.tsx': 1,
}

for (const [file, count] of Object.entries(EXPECTED)) {
  test(`${file}: ${count} action row(s) carry data-modal-footer as an attribute on the existing div`, () => {
    const text = src(file)
    expect(markers(text)).toBe(count)
    expect(anyMarker(text)).toBe(count)
  })
}

// No single action-row wrapper: left alone on purpose.
for (const file of [
  'components/jobs/JobCorrectionModal.tsx', 'components/inventory/BulkReceive.tsx', 'components/inventory/StockReliability.tsx',
  'components/inventory/PurchaseEvidenceModal.tsx', 'components/orders/ProductionHistory.tsx', 'components/education/ReviewDecisionModal.tsx',
  'components/vision/TrainingDataset.tsx',
]) {
  test(`${file}: no marker (buttons are not in one wrapper)`, () => {
    expect(anyMarker(src(file))).toBe(0)
  })
}

test('CameraModal passes ownHeader to the shared Modal', () => {
  expect(src('components/printers/CameraModal.tsx')).toMatch(/<Modal\s[^>]*\bownHeader\b/)
})
