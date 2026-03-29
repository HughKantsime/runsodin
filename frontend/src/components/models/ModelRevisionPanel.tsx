import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { History, Upload, X, RotateCcw } from 'lucide-react'
import { modelRevisions } from '../../api'
import { canDo } from '../../permissions'

export default function ModelRevisionPanel({ modelId, modelName, onClose }: { modelId: number; modelName?: string; onClose: () => void }) {
  const queryClient = useQueryClient()
  const [showUpload, setShowUpload] = useState(false)
  const [changelog, setChangelog] = useState('')
  const [file, setFile] = useState(null)

  const { data: revisions, isLoading } = useQuery({
    queryKey: ['model-revisions', modelId],
    queryFn: () => modelRevisions.list(modelId),
    enabled: !!modelId,
  })

  const createRevision = useMutation({
    mutationFn: () => modelRevisions.create(modelId, changelog, file),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['model-revisions', modelId] })
      setShowUpload(false)
      setChangelog('')
      setFile(null)
    },
  })

  const revertRevision = useMutation({
    mutationFn: (revNumber) => modelRevisions.revert(modelId, revNumber),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['model-revisions', modelId] }),
  })

  return (
    <div className="fixed inset-0 bg-black/50 flex items-end sm:items-center justify-center z-50 p-0 sm:p-4" role="dialog" aria-modal="true" aria-labelledby="revision-panel-title">
      <div className="bg-[var(--brand-card-bg)] rounded-t-xl sm:rounded w-full max-w-lg p-4 sm:p-6 border border-[var(--brand-card-border)] max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <History size={18} className="text-[var(--brand-primary)]" />
            <h2 id="revision-panel-title" className="text-lg font-display font-semibold">Revisions</h2>
            <span className="text-sm text-[var(--brand-muted)]">— {modelName}</span>
          </div>
          <button onClick={onClose} className="text-[var(--brand-muted)] hover:text-[var(--brand-text-secondary)]" aria-label="Close revisions"><X size={20} /></button>
        </div>

        {canDo('models.edit') && !showUpload && (
          <button
            onClick={() => setShowUpload(true)}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-[var(--brand-primary)] hover:bg-[var(--brand-primary-hover)] rounded-md text-sm mb-4"
          >
            <Upload size={14} /> Upload New Revision
          </button>
        )}

        {showUpload && (
          <div className="mb-4 p-3 bg-[var(--brand-input-bg)] rounded-md border border-[var(--brand-card-border)] space-y-3">
            <div>
              <label htmlFor="rev-file" className="text-xs text-[var(--brand-muted)] block mb-1">File (.3mf)</label>
              <input id="rev-file" type="file" accept=".3mf" onChange={e => setFile(e.target.files?.[0] || null)}
                className="text-sm text-[var(--brand-text-secondary)]" />
            </div>
            <div>
              <label htmlFor="rev-changelog" className="text-xs text-[var(--brand-muted)] block mb-1">Changelog</label>
              <textarea id="rev-changelog" value={changelog} onChange={e => setChangelog(e.target.value)}
                placeholder="What changed in this revision..."
                className="w-full bg-[var(--brand-card-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-sm resize-none" rows={2} />
            </div>
            <div className="flex gap-2">
              <button onClick={() => createRevision.mutate()} disabled={!changelog.trim() || createRevision.isPending}
                className="px-3 py-1.5 bg-[var(--brand-primary)] hover:bg-[var(--brand-primary-hover)] disabled:opacity-50 rounded-md text-sm">
                {createRevision.isPending ? 'Uploading...' : 'Save Revision'}
              </button>
              <button onClick={() => setShowUpload(false)} className="px-3 py-1.5 bg-[var(--brand-surface)] hover:bg-[var(--brand-card-border)] rounded-md text-sm">Cancel</button>
            </div>
          </div>
        )}

        {isLoading && <p className="text-sm text-[var(--brand-muted)] py-4">Loading revisions...</p>}

        {!isLoading && (!revisions || revisions.length === 0) && (
          <p className="text-sm text-[var(--brand-muted)] py-4">No revisions recorded yet.</p>
        )}

        <div className="divide-y divide-[var(--brand-card-border)]">
        {revisions?.map((rev, idx) => (
          <div key={rev.id} className="py-3 first:pt-0">
            <div className="flex items-center justify-between mb-1">
              <span className="text-sm font-medium text-[var(--brand-text)]">v{rev.revision_number}</span>
              <div className="flex items-center gap-2">
                {idx > 0 && canDo('models.edit') && (
                  <button
                    onClick={() => revertRevision.mutate(rev.revision_number)}
                    disabled={revertRevision.isPending}
                    className="flex items-center gap-1 px-2 py-0.5 text-xs text-amber-400 hover:bg-amber-900/30 rounded"
                    title={`Revert to v${rev.revision_number}`}
                  >
                    <RotateCcw size={12} /> Revert
                  </button>
                )}
                <span className="text-xs text-[var(--brand-muted)] font-mono">{rev.created_at ? new Date(rev.created_at).toLocaleDateString() : ''}</span>
              </div>
            </div>
            {rev.changelog && <p className="text-xs text-[var(--brand-muted)]">{rev.changelog}</p>}
            {rev.uploaded_by_name && <p className="text-xs text-[var(--brand-muted)] mt-1">by {rev.uploaded_by_name}</p>}
          </div>
        ))}
        </div>
      </div>
    </div>
  )
}
