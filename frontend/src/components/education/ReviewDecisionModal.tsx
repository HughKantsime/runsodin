import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, Printer, XCircle } from 'lucide-react'
import toast from 'react-hot-toast'
import { education } from '../../api'
import type { EducationSubmission } from '../../types'
import { Button, Modal, Select, Textarea } from '../ui'

interface ReviewDecisionModalProps {
  submission: EducationSubmission | null
  onClose: () => void
}

export default function ReviewDecisionModal({ submission, onClose }: ReviewDecisionModalProps) {
  const queryClient = useQueryClient()
  const [printerId, setPrinterId] = useState('')
  const [reason, setReason] = useState('')
  const [error, setError] = useState('')

  const printerQuery = useQuery({
    queryKey: ['education-center-printers', submission?.cost_center_id],
    queryFn: () => education.listCenterPrinters(submission!.cost_center_id),
    enabled: !!submission,
  })
  const printers = printerQuery.data?.items || []

  useEffect(() => {
    if (!submission) return
    setPrinterId('')
    setReason('')
    setError('')
  }, [submission])

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

  const finish = async (message: string) => {
    toast.success(message)
    await queryClient.invalidateQueries({ queryKey: ['education-submissions'] })
    await queryClient.invalidateQueries({ queryKey: ['education-centers'] })
    onClose()
  }

  const approve = useMutation({
    mutationFn: () => education.approveSubmission(submission!.id, {
      revision: submission!.lifecycle_revision,
      printer_id: Number(printerId),
      command_id: crypto.randomUUID(),
    }),
    onSuccess: () => finish('Submission approved and queued'),
    onError: (mutationError: Error) => setError(mutationError.message),
  })
  const reject = useMutation({
    mutationFn: () => education.rejectSubmission(submission!.id, {
      revision: submission!.lifecycle_revision,
      reason,
      command_id: crypto.randomUUID(),
    }),
    onSuccess: () => finish('Submission returned to the student'),
    onError: (mutationError: Error) => setError(mutationError.message),
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
            <Select
              label="Authorized printer"
              value={printerId}
              onChange={(event) => {
                setPrinterId(event.target.value)
                setError('')
              }}
              disabled={printerQuery.isLoading}
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
                    ? 'Compatible — ready for approval'
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
            <Button
              className="w-full sm:w-auto"
              onClick={() => {
                setError('')
                approve.mutate()
              }}
              loading={approve.isPending}
              disabled={
                !selectedPrinter
                || reject.isPending
                || compatibilityQuery.isLoading
                || !compatibilityQuery.data?.compatible
              }
              icon={CheckCircle2}
            >
              Approve and queue
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
                reject.mutate()
              }}
              loading={reject.isPending}
              disabled={!reason.trim() || approve.isPending}
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
