import { useEffect, useMemo, useRef, useState } from 'react'
import { FileUp, ShieldCheck } from 'lucide-react'
import toast from 'react-hot-toast'
import { auth, education } from '../../api'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import type { EducationCostCenter, EducationSubmission, User } from '../../types'
import { Button, Modal, Select } from '../ui'

export function educationAudience(user: Pick<User, 'id' | 'username' | 'role' | 'group_id'> | null | undefined) {
  return user && Number.isInteger(user.id) && (user.id ?? 0) > 0
    ? JSON.stringify([user.id, user.username, user.role, user.group_id ?? null]) : ''
}

interface SubmissionUploadModalProps {
  open: boolean
  onClose: () => void
  centers: EducationCostCenter[]
  onCreated: () => void
  revision?: EducationSubmission | null
  audience: string
}

export default function SubmissionUploadModal({
  open,
  onClose,
  centers,
  onCreated,
  revision,
  audience,
}: SubmissionUploadModalProps) {
  const queryClient = useQueryClient()
  const { data: principal } = useQuery<User | null>({ queryKey: ['auth', 'me'], queryFn: auth.me, enabled: false })
  const liveAudience = educationAudience(principal)
  const currentAudience = () => educationAudience(queryClient.getQueryData<User | null>(['auth', 'me']))
  const [centerId, setCenterId] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [error, setError] = useState('')

  const [pending, setPending] = useState(false)
  const generation = useRef(0)
  const signature = `${open}:${audience}:${liveAudience}:${revision?.id ?? ''}`
  const previous = useRef(signature)
  const pendingAttempt = useRef<number | null>(null)
  const token = useRef<{ file: File; center: number; audience: string; value: string } | null>(null)
  const latest = useRef({ open, audience, centers })
  latest.current = { open, audience, centers }
  if (previous.current !== signature) {
    previous.current = signature
    generation.current += 1
    pendingAttempt.current = null
  }
  useEffect(() => {
    setPending(false)
    if (!open) return
    setCenterId(revision ? String(revision.cost_center_id) : (centers[0] ? String(centers[0].id) : ''))
    setFile(null)
    setError('')
    token.current = null
    // A center refetch must preserve the selected File, retry token and error.
  }, [open, audience, liveAudience, revision?.id])
  useEffect(() => () => { generation.current += 1 }, [])

  const selectedCenter = useMemo(
    () => centers.find((center) => center.active && String(center.id) === centerId),
    [centers, centerId],
  )
  const close = () => {
    generation.current += 1
    pendingAttempt.current = null
    latest.current.open = false
    onClose()
  }
  const submit = async () => {
    if (pendingAttempt.current !== null) return
    const owner = currentAudience()
    if (!open || !owner || owner !== audience || !file || !selectedCenter
      || !latest.current.centers.some(center => center.active && center.id === selectedCenter.id)) {
      setError('Choose a currently authorized class and sliced .3mf file.')
      return
    }
    if (!token.current || token.current.file !== file || token.current.center !== selectedCenter.id || token.current.audience !== owner) {
      token.current = { file, center: selectedCenter.id, audience: owner, value: crypto.randomUUID() }
    }
    const attempt = { generation: generation.current, owner, center: selectedCenter.id, file, token: token.current.value }
    const current = () => generation.current === attempt.generation && latest.current.open
      && latest.current.audience === attempt.owner && currentAudience() === attempt.owner
      && latest.current.centers.some(center => center.active && center.id === attempt.center)
    pendingAttempt.current = attempt.generation
    setPending(true)
    setError('')
    try {
      if (!current()) return
      await education.uploadSubmission(attempt.center, attempt.file, attempt.token)
      if (!current()) return
      toast.success('Submission sent for teacher review')
      onCreated()
      close()
    } catch (mutationError) {
      if (current()) setError(mutationError instanceof Error ? mutationError.message : 'Upload failed. Try again.')
    } finally {
      if (generation.current === attempt.generation) {
        pendingAttempt.current = null
        setPending(false)
      }
    }
  }

  const chooseFile = (next: File | null) => {
    if (pendingAttempt.current !== null) return
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
    <Modal isOpen={open && !!liveAudience && liveAudience === audience} onClose={close} title={revision ? 'Revise and resubmit' : 'Submit a print'} size="lg">
      <form
        className="space-y-5"
        onSubmit={(event) => {
          event.preventDefault()
          void submit()
        }}
      >
        <div className="flex gap-3 rounded-md bg-[var(--brand-surface)] p-3">
          <ShieldCheck size={18} className="mt-0.5 shrink-0 text-[var(--brand-primary)]" />
          <div>
            <p className="text-sm font-medium text-[var(--brand-text-primary)]">Teacher approval required</p>
            <p className="mt-1 text-xs leading-relaxed text-[var(--brand-text-secondary)]">
              Upload a sliced Bambu 3MF from OrcaSlicer. ODIN checks the file, then your teacher
              selects an authorized compatible printer. Raw models and K1 G-code are not accepted
              in this pilot flow.
            </p>
          </div>
        </div>

        {revision && <div className="rounded-md border border-[var(--brand-card-border)] p-3 text-sm">
          <p>Previous submission: {revision.item_name}</p>
          <p className="mt-1">{revision.rejection_reason}</p>
          <p className="mt-2">Select a revised sliced file for a new teacher review. The rejected submission stays in your history.</p>
        </div>}
        <Select
          label="Class or club"
          value={centerId}
          onChange={(event) => { if (pendingAttempt.current === null) setCenterId(event.target.value) }}
          disabled={pending}
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
              disabled={pending}
            />
          </label>
        </div>

        {error && (
          <p role="alert" className="text-sm text-[var(--status-failed)]">
            {error}
          </p>
        )}

        <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <Button type="button" variant="ghost" onClick={close}>Cancel</Button>
          <Button type="submit" loading={pending} disabled={!file || !selectedCenter || !audience || audience !== currentAudience()}>
            Submit for review
          </Button>
        </div>
      </form>
    </Modal>
  )
}
