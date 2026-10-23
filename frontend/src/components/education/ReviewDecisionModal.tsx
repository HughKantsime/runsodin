import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, Printer, XCircle } from 'lucide-react'
import toast from 'react-hot-toast'
import SubmissionSliceInfo from './SubmissionSliceInfo'
import { education } from '../../api'
import type { EducationSubmission, EducationAMSReport, EducationApprovalRequest } from '../../types'
import { Button, Modal, Select, Textarea } from '../ui'

interface ReviewDecisionModalProps {
  audience?: string
  submission: EducationSubmission | null
  onClose: () => void
}

export default function ReviewDecisionModal({ submission, onClose, audience = '' }: ReviewDecisionModalProps) {
  const queryClient = useQueryClient()
  const [printerId, setPrinterId] = useState('')
  const [reason, setReason] = useState('')
  const [error, setError] = useState('')
  const [filamentSupply, setFilamentSupply] = useState('')
  const [tray, setTray] = useState('')
  const [amsReport, setAMSReport] = useState<EducationAMSReport | null>(null)
  const [reportFresh, setReportFresh] = useState(false)
  const approvalRequest = useRef<EducationApprovalRequest | null>(null)
  const savedApprovals = useRef(new Map<string, EducationApprovalRequest>())
  const readBusy = useRef(false)
  const approvalBusy = useRef(false)
  const identity = `${submission?.id}:${submission?.lifecycle_revision}`
  const operationContext = useRef({ identity, epoch: 0 })
  if (operationContext.current.identity !== identity) {
    operationContext.current = { identity, epoch: operationContext.current.epoch + 1 }
  }
  const submissionScope = `${identity}:${operationContext.current.epoch}`
  const currentSubmissionScope = useRef(submissionScope)
  currentSubmissionScope.current = submissionScope
  useEffect(() => () => { currentSubmissionScope.current = ''; currentReadScope.current = '' }, [])
  const [approvalAttempted, setApprovalAttempted] = useState(false)

  useEffect(() => {
    if (!amsReport) { setReportFresh(false); return }
    setReportFresh(true)
    // The server checks its own receipt age. Leave room for network and staging.
    const timer = setTimeout(() => setReportFresh(false), 110_000)
    return () => clearTimeout(timer)
  }, [amsReport])

  const printerQuery = useQuery({
    queryKey: ['education-center-printers', submission?.cost_center_id],
    queryFn: () => education.listCenterPrinters(submission!.cost_center_id),
    enabled: !!submission,
  })
  const printers = printerQuery.data?.items || []

  useEffect(() => {
    setPrinterId('')
    setReason('')
    setError('')
    setFilamentSupply('')
    setTray('')
    setAMSReport(null)
    approvalRequest.current = savedApprovals.current.get(identity) || null
    setApprovalAttempted(!!approvalRequest.current)
    if (approvalRequest.current) {
      setPrinterId(String(approvalRequest.current.printer_id))
      setFilamentSupply(approvalRequest.current.ams_mapping ? 'ams' : 'default')
      setTray(String(approvalRequest.current.ams_mapping?.[0].target_slot || ''))
    }
  }, [submission?.id, submission?.lifecycle_revision])

  const selectedPrinter = useMemo(
    () => printers.find((printer) => String(printer.printer_id) === printerId),
    [printerId, printers],
  )
  const compatibilityQuery = useQuery({
    queryKey: [
      'education-compatibility',
      submission?.id,
      submission?.lifecycle_revision,
      printerId,
    ],
    queryFn: () => education.previewCompatibility(
      submission!.id,
      Number(printerId),
      submission!.lifecycle_revision,
    ),
    enabled: !!submission && !!selectedPrinter,
  })

  const finish = async (message: string, scope = currentSubmissionScope.current) => {
    if (scope !== currentSubmissionScope.current) return
    toast.success(message)
    await queryClient.invalidateQueries({ queryKey: ['education-submissions'] })
    await queryClient.invalidateQueries({ queryKey: ['education-centers'] })
    if (scope === currentSubmissionScope.current) onClose()
  }

  const isBambu = selectedPrinter?.api_type === 'bambu'
  const usesAMS = isBambu && filamentSupply === 'ams'
  const readScope = `${submissionScope}:${printerId}:${tray}:${filamentSupply}`
  const currentReadScope = useRef(readScope)
  currentReadScope.current = readScope
  const readAMS = useMutation({
    mutationFn: async (selection: { id: number; revision: number; printer: number; tray: number; scope: string }) => {
      const report = await education.readAMS(selection.id, {
        revision: selection.revision, printer_id: selection.printer, selected_slots: [selection.tray],
      })
      if (!report) throw new Error('Tray read unavailable. Sign in again and refresh the queue.')
      return report
    },
    onSuccess: (report, selection) => {
      if (selection.scope !== currentReadScope.current) return
      setAMSReport(report); setError('')
    },
    onError: (mutationError: Error, selection) => {
      if (selection.scope !== currentReadScope.current) return
      setAMSReport(null); setError(mutationError.message)
    },
    onSettled: () => { readBusy.current = false },
  })

  const approve = useMutation({
    mutationFn: async (operation: { id: number; body: EducationApprovalRequest; scope: string }) => {
      const result = await education.approveSubmission(operation.id, operation.body)
      const expected = operation.body
      if (!result || result.id !== operation.id || result.approved_printer_id !== expected.printer_id
          || (expected.ams_mapping && (
            JSON.stringify(result.ams_mapping) !== JSON.stringify(expected.ams_mapping.map((entry) => entry.target_slot - 1))
            || result.ams_generation_id !== expected.ams_generation_id || result.plate_index !== expected.plate_index
            || !Number.isInteger(result.print_file_id) || !(result.print_file_id! > 0)
            || !/^[a-f0-9]{64}$/.test(result.payload_sha256 || '')
          ))) {
        throw new Error('Approval result unconfirmed. Retry the same request or refresh the queue.')
      }
      return result
    },
    onSuccess: async (_, operation) => {
      await queryClient.invalidateQueries({ queryKey: ['education-submissions'] })
      if (operation.scope !== currentSubmissionScope.current) return
      await finish('Submission approved and queued', operation.scope)
    },
    onError: (mutationError: Error, operation) => {
      if (operation.scope === currentSubmissionScope.current) setError(mutationError.message)
    },
    onSettled: () => { approvalBusy.current = false },
  })

  const reject = useMutation({
    mutationFn: (operation: { id: number; revision: number; reason: string; scope: string }) =>
      education.rejectSubmission(operation.id, {
        revision: operation.revision, reason: operation.reason, command_id: crypto.randomUUID(),
      }),
    onSuccess: (_, operation) => finish('Submission returned to the student', operation.scope),
    onError: (mutationError: Error, operation) => {
      if (operation.scope === currentSubmissionScope.current) setError(mutationError.message)
    },
  })

  return (
    <Modal isOpen={!!submission} onClose={onClose} title="Review submission" size="lg">
      {submission && (
        <div className="space-y-5">
          <div className="rounded-md bg-[var(--brand-surface)] p-4">
            <p className="font-medium text-[var(--brand-text-primary)]">{submission.item_name}</p>
            <p className="mt-1 text-xs text-[var(--brand-text-secondary)]">
              Submitted by {submission.submitter_username} · revision {submission.lifecycle_revision}
            </p>
          </div>

          <div className="space-y-3">
            <div className="flex items-center gap-2">
              <Printer size={16} className="text-[var(--brand-primary)]" />
              <h3 className="text-sm font-semibold text-[var(--brand-text-primary)]">Approve to a printer</h3>
            </div>
            <SubmissionSliceInfo audience={audience} id={submission.id} revision={submission.lifecycle_revision} printerId={printerId ? Number(printerId) : undefined} />
        <Select
              label="Authorized printer"
              value={printerId}
              onChange={(event) => {
                setPrinterId(event.target.value)
                setFilamentSupply('')
                setTray('')
                setAMSReport(null)
                setError('')
              }}
              disabled={printerQuery.isLoading || readAMS.isPending || approvalAttempted}
            >
              <option value="">Select a printer</option>
              {printers.map((printer) => (
                <option key={printer.printer_id} value={printer.printer_id}>
                  {printer.name} · {printer.machine_type || printer.api_type || 'Unknown model'}
                </option>
              ))}
            </Select>
            {selectedPrinter && compatibilityQuery.isLoading && (
              <p role="status" className="text-xs text-[var(--brand-text-secondary)]">
                Checking file, bed, nozzle, material, and protocol compatibility…
              </p>
            )}
            {compatibilityQuery.data && (
              <div
                className="rounded-md bg-[var(--brand-surface)] p-3 text-xs"
                role="status"
              >
                <p className={compatibilityQuery.data.compatible
                  ? 'text-[var(--status-completed)]'
                  : 'text-[var(--status-failed)]'}
                >
                  {compatibilityQuery.data.compatible
                    ? 'File and printer are compatible'
                    : 'This printer is not compatible'}
                </p>
                {compatibilityQuery.data.reasons.length > 0 && (
                  <ul className="mt-2 list-disc space-y-1 pl-4 text-[var(--brand-text-secondary)]">
                    {compatibilityQuery.data.reasons.map((item) => <li key={item}>{item}</li>)}
                  </ul>
                )}
              </div>
            )}
            {compatibilityQuery.error && (
              <p role="alert" className="text-xs text-[var(--status-failed)]">
                {compatibilityQuery.error.message}
              </p>
            )}
            {isBambu && (
              <div className="space-y-3">
                <Select label="Filament supply" value={filamentSupply}
                  disabled={readAMS.isPending || approvalAttempted}
                  onChange={(event) => {
                    setFilamentSupply(event.target.value)
                    setAMSReport(null)
                    setError('')
                  }}>
                  <option value="">Select filament supply</option>
                  <option value="default">Printer default (no explicit AMS selection)</option>
                  <option value="ams">AMS tray</option>
                </Select>
                {filamentSupply === 'default' && (
                  <p className="text-xs text-[var(--brand-text-secondary)]">
                    This keeps the existing printer-default workflow. It does not select a physical tray
                    or switch the printer to an external spool.
                  </p>
                )}
                {usesAMS && (
                  <>
                    <p className="text-xs text-[var(--brand-text-secondary)]">
                      AMS approval currently supports one plate and one filament source.
                      Select the physical tray containing that filament. Other files need a supported workflow.
                    </p>
                    <Select label="Physical AMS tray" value={tray}
                      disabled={readAMS.isPending || approvalAttempted}
                      onChange={(event) => { setTray(event.target.value); setAMSReport(null); setError('') }}>
                      <option value="">Select tray number</option>
                      {Array.from({ length: 16 }, (_, index) => (
                        <option key={index + 1} value={index + 1}>Tray {index + 1}</option>
                      ))}
                    </Select>
                    <Button onClick={() => { if (readBusy.current) return; readBusy.current = true; setAMSReport(null); setError(''); readAMS.mutate({ id: submission.id, revision: submission.lifecycle_revision, printer: Number(printerId), tray: Number(tray), scope: readScope }) }}
                      loading={readAMS.isPending} disabled={!tray || approvalAttempted || reject.isPending}>
                      Read selected tray
                    </Button>
                    {amsReport && (
                      <p role="status" className="text-xs text-[var(--brand-text-secondary)]">
                        Tray {tray}: {amsReport.trays.find((item) => item.slot_number === Number(tray))?.material || 'Unknown material'}.
                        {' '}{reportFresh ? 'Current report received.' : 'Report expired. Read the tray again.'}
                        {' '}Confirm filament is loaded and routed correctly; the report does not prove mechanical readiness.
                      </p>
                    )}
                  </>
                )}
              </div>
            )}
            {approvalAttempted && error && (
              <p className="text-xs text-[var(--brand-text-secondary)]">
                Retry uses the same approval request. If it was saved, the server returns that result.
                Close and refresh the queue before making a different decision.
              </p>
            )}
            <Button
              className="w-full sm:w-auto"
              onClick={() => {
                if (approvalBusy.current) return
                approvalBusy.current = true
                setError('')
                if (!approvalRequest.current) {
                  approvalRequest.current = {
                    revision: submission.lifecycle_revision, printer_id: Number(printerId),
                    command_id: crypto.randomUUID(),
                    ...(usesAMS ? {
                      ams_mapping: [{ source_slot: 0, target_slot: Number(tray) }],
                      ams_generation_id: amsReport!.generation_id, plate_index: 0,
                    } : {}),
                  }
                  savedApprovals.current.set(identity, approvalRequest.current)
                }
                setApprovalAttempted(true)
                approve.mutate({ id: submission.id, body: approvalRequest.current, scope: submissionScope })
              }}
              loading={approve.isPending}
              disabled={
                (!approvalAttempted && !selectedPrinter)
                || readAMS.isPending
                || (!approvalAttempted && isBambu && !filamentSupply)
                || (!approvalAttempted && usesAMS && (!reportFresh || !amsReport?.trays.some((item) => item.slot_number === Number(tray) && item.material_state === 'reported' && !!item.material)))
                || reject.isPending
                || (!approvalAttempted && compatibilityQuery.isLoading)
                || (!approvalAttempted && !compatibilityQuery.data?.compatible)
              }
              icon={CheckCircle2}
            >
              {approvalAttempted ? 'Retry approval' : 'Approve and queue'}
            </Button>
          </div>

          <div className="border-t border-[var(--brand-card-border)] pt-4">
            <div className="mb-3 flex items-center gap-2">
              <XCircle size={16} className="text-[var(--status-failed)]" />
              <h3 className="text-sm font-semibold text-[var(--brand-text-primary)]">Return to student</h3>
            </div>
            <Textarea
              label="Reason"
              value={reason}
              maxLength={1000}
              rows={3}
              onChange={(event) => {
                setReason(event.target.value)
                setError('')
              }}
              placeholder="Explain what needs to change before resubmission."
            />
            <Button
              variant="danger"
              className="mt-3 w-full sm:w-auto"
              onClick={() => {
                setError('')
                reject.mutate({ id: submission.id, revision: submission.lifecycle_revision, reason, scope: submissionScope })
              }}
              loading={reject.isPending}
              disabled={!reason.trim() || approve.isPending || readAMS.isPending || approvalAttempted}
            >
              Reject submission
            </Button>
          </div>

          {error && <p role="alert" className="text-sm text-[var(--status-failed)]">{error}</p>}
        </div>
      )}
    </Modal>
  )
}
