import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { History, Upload, X, RotateCcw } from 'lucide-react'
import { modelRevisions } from '../api'
import { canDo } from '../permissions'

export default function ModelRevisionPanel({ modelId, modelName, onClose }) {
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
      <div className="bg-farm-900 rounded-t-xl sm:rounded w-full max-w-lg p-4 sm:p-6 border border-farm-700 max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <History size={18} className="text-print-400" />
            <h2 id="revision-panel-title" className="text-lg font-display font-semibold">Revisions</h2>
            <span className="text-sm text-farm-400">— {modelName}</span>
          </div>
          <button onClick={onClose} className="text-farm-500 hover:text-farm-300" aria-label="Close revisions"><X size={20} /></button>
        </div>

        {canDo('models.edit') && !showUpload && (
          <button
            onClick={() => setShowUpload(true)}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-print-600 hover:bg-print-500 rounded-lg text-sm mb-4"
          >
            <Upload size={14} /> Upload New Revision
          </button>
        )}

        {showUpload && (
          <div className="mb-4 p-3 bg-farm-800 rounded-lg border border-farm-700 space-y-3">
            <div>
              <label htmlFor="rev-file" className="text-xs text-farm-400 block mb-1">File (.3mf)</label>
              <input id="rev-file" type="file" accept=".3mf" onChange={e => setFile(e.target.files?.[0] || null)}
                className="text-sm text-farm-300" />
            </div>
            <div>
              <label htmlFor="rev-changelog" className="text-xs text-farm-400 block mb-1">Changelog</label>
              <textarea id="rev-changelog" value={changelog} onChange={e => setChangelog(e.target.value)}
                placeholder="What changed in this revision..."
                className="w-full bg-farm-900 border border-farm-700 rounded-lg px-3 py-2 text-sm resize-none" rows={2} />
            </div>
            <div className="flex gap-2">
              <button onClick={() => createRevision.mutate()} disabled={!changelog.trim() || createRevision.isPending}
                className="px-3 py-1.5 bg-print-600 hover:bg-print-500 disabled:opacity-50 rounded-lg text-sm">
                {createRevision.isPending ? 'Uploading...' : 'Save Revision'}
              </button>
              <button onClick={() => setShowUpload(false)} className="px-3 py-1.5 bg-farm-700 hover:bg-farm-600 rounded-lg text-sm">Cancel</button>
            </div>
          </div>
        )}

        {isLoading && <p className="text-sm text-farm-500 py-4">Loading revisions...</p>}

        {!isLoading && (!revisions || revisions.length === 0) && (
          <p className="text-sm text-farm-500 py-4">No revisions recorded yet.</p>
        )}

        {revisions?.map((rev, idx) => (
          <div key={rev.id} className="p-3 bg-farm-800 rounded-lg mb-2 border border-farm-700">
            <div className="flex items-center justify-between mb-1">
              <span className="text-sm font-medium text-farm-100">v{rev.revision_number}</span>
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
                <span className="text-xs text-farm-500">{rev.created_at ? new Date(rev.created_at).toLocaleDateString() : ''}</span>
              </div>
            </div>
            {rev.changelog && <p className="text-xs text-farm-400">{rev.changelog}</p>}
            {rev.uploaded_by_name && <p className="text-xs text-farm-600 mt-1">by {rev.uploaded_by_name}</p>}
          </div>
        ))}
      </div>
    </div>
  )
}
