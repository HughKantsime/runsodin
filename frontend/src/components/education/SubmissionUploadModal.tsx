import { useEffect, useMemo, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { FileUp, ShieldCheck } from 'lucide-react'
import toast from 'react-hot-toast'
import { education } from '../../api'
import type { EducationCostCenter } from '../../types'
import { Button, Modal, Select } from '../ui'

interface SubmissionUploadModalProps {
  open: boolean
  onClose: () => void
  centers: EducationCostCenter[]
  onCreated: () => void
}

export default function SubmissionUploadModal({
  open,
  onClose,
  centers,
  onCreated,
}: SubmissionUploadModalProps) {
  const [centerId, setCenterId] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!open) return
    setCenterId(centers[0] ? String(centers[0].id) : '')
    setFile(null)
    setError('')
  }, [open, centers])

  const selectedCenter = useMemo(
    () => centers.find((center) => String(center.id) === centerId),
    [centers, centerId],
  )

  const upload = useMutation({
    mutationFn: () => {
      if (!file || !selectedCenter) throw new Error('Choose a cost center and sliced .3mf file.')
      return education.uploadSubmission(selectedCenter.id, file)
    },
    onSuccess: () => {
      toast.success('Submission sent for teacher review')
      onCreated()
      onClose()
    },
    onError: (mutationError: Error) => setError(mutationError.message),
  })

  const chooseFile = (next: File | null) => {
    setError('')
    if (!next) return setFile(null)
    if (!next.name.toLowerCase().endsWith('.3mf')) {
      setFile(null)
      setError('Education submissions must be a sliced Bambu .3mf file.')
      return
    }
    setFile(next)
  }

  return (
    <Modal isOpen={open} onClose={onClose} title="Submit a print" size="lg">
      <form
        className="space-y-5"
        onSubmit={(event) => {
          event.preventDefault()
          setError('')
          upload.mutate()
        }}
      >
        <div className="flex gap-3 rounded-md bg-[var(--brand-surface)] p-3">
          <ShieldCheck size={18} className="mt-0.5 flex-shrink-0 text-[var(--brand-primary)]" />
          <div>
            <p className="text-sm font-medium text-[var(--brand-text-primary)]">Teacher approval required</p>
            <p className="mt-1 text-xs leading-relaxed text-[var(--brand-text-secondary)]">
              Upload a sliced Bambu 3MF from OrcaSlicer. ODIN checks the file, then your teacher
              selects an authorized compatible printer. Raw models and K1 G-code are not accepted
              in this pilot flow.
            </p>
          </div>
        </div>

        <Select
          label="Class or club"
          value={centerId}
          onChange={(event) => setCenterId(event.target.value)}
          options={centers.map((center) => ({
            value: String(center.id),
            label: `${center.name} · ${center.code}`,
          }))}
          required
        />

        <div>
          <label htmlFor="education-file" className="mb-1 block text-sm text-[var(--brand-text-secondary)]">
            Sliced Bambu 3MF
          </label>
          <label
            htmlFor="education-file"
            className="flex min-h-28 cursor-pointer flex-col items-center justify-center gap-2 rounded-md border border-dashed border-[var(--brand-input-border)] bg-[var(--brand-input-bg)] px-4 py-5 text-center transition-colors hover:border-[var(--brand-primary)] focus-within:border-[var(--brand-primary)]"
          >
            <FileUp size={24} className="text-[var(--brand-primary)]" />
            <span className="text-sm font-medium text-[var(--brand-text-primary)]">
              {file ? file.name : 'Choose a .3mf file'}
            </span>
            <span className="text-xs text-[var(--brand-text-muted)]">
              {file ? `${(file.size / 1024 / 1024).toFixed(2)} MiB` : 'Maximum retained upload: 100 MiB'}
            </span>
            <input
              id="education-file"
              type="file"
              accept=".3mf,model/3mf,application/vnd.ms-package.3dmanufacturing-3dmodel+xml"
              className="sr-only"
              onChange={(event) => chooseFile(event.target.files?.[0] || null)}
              required
            />
          </label>
        </div>

        {error && (
          <p role="alert" className="text-sm text-[var(--status-failed)]">
            {error}
          </p>
        )}

        <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" loading={upload.isPending} disabled={!file || !selectedCenter}>
            Submit for review
          </Button>
        </div>
      </form>
    </Modal>
  )
}
