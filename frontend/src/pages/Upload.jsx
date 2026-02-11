import { useState, useCallback } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Upload as UploadIcon, FileUp, Clock, Scale, Layers, Check, Printer, Trash2, ArrowRight, Calendar, CheckSquare, Square, Zap } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import clsx from 'clsx'

import { printFiles, getApprovalSetting } from '../api'
import { models as modelsApi } from '../api'

function DropZone({ onFileSelect, isUploading }) {
  const [isDragging, setIsDragging] = useState(false)
  
  const handleDrag = useCallback((e) => { e.preventDefault(); e.stopPropagation() }, [])
  const handleDragIn = useCallback((e) => { e.preventDefault(); e.stopPropagation(); setIsDragging(true) }, [])
  const handleDragOut = useCallback((e) => { e.preventDefault(); e.stopPropagation(); setIsDragging(false) }, [])
  const handleDrop = useCallback((e) => {
    e.preventDefault(); e.stopPropagation(); setIsDragging(false)
    const files = e.dataTransfer?.files
    if (files && files.length > 0) {
      const file = files[0]
      if (file.name.endsWith('.3mf')) onFileSelect(file)
    }
  }, [onFileSelect])
  
  const handleFileInput = (e) => { const file = e.target.files?.[0]; if (file) onFileSelect(file) }
  
  return (
    <div
      className={clsx(
        'border-2 border-dashed rounded p-8 md:p-12 text-center transition-all',
        isDragging ? 'border-print-500 bg-print-900/20' : 'border-farm-700 hover:border-farm-500',
        isUploading && 'opacity-50 pointer-events-none'
      )}
      onDragEnter={handleDragIn}
      onDragLeave={handleDragOut}
      onDragOver={handleDrag}
      onDrop={handleDrop}
    >
      <input type="file" accept=".3mf" onChange={handleFileInput} className="hidden" id="file-upload" disabled={isUploading} />
      <label htmlFor="file-upload" className="cursor-pointer">
        <div className="flex flex-col items-center gap-3 md:gap-4">
          <div className={clsx('p-3 md:p-4 rounded-full', isDragging ? 'bg-print-600' : 'bg-farm-800')}>
            <FileUp size={36} className={isDragging ? 'text-white' : 'text-farm-400'} />
          </div>
          <div>
            <p className="text-base md:text-lg font-medium">{isUploading ? 'Uploading...' : 'Drop .3mf file here'}</p>
            <p className="text-farm-500 text-sm mt-1">or click to browse</p>
          </div>
        </div>
      </label>
    </div>
  )
}

function FilamentBadge({ filament }) {
  return (
    <div className="flex items-center gap-2 bg-farm-800 rounded-lg px-3 py-2">
      <div className="w-4 h-4 rounded-full border border-farm-600" style={{ backgroundColor: filament.color }} />
      <span className="text-sm">{filament.type}</span>
      <span className="text-xs text-farm-500">{filament.used_grams}g</span>
    </div>
  )
}

function UploadSuccess({ data, onUploadAnother, onViewLibrary, onScheduleNow, onUpdateObjects, submitForApproval, onSaveQuantity }) {
  const [quickPrinted, setQuickPrinted] = useState(false)
  const quickPrintMut = useMutation({
    mutationFn: () => modelsApi.schedule(data.model_id),
    onSuccess: () => setQuickPrinted(true),
  })

  return (
    <div className="bg-farm-900 rounded border border-farm-800 overflow-hidden">
      <div className="flex flex-col md:flex-row">
        {/* Thumbnail */}
        <div className="w-full md:w-64 h-48 md:h-64 bg-farm-950 flex-shrink-0">
          {data.thumbnail_b64 ? (
            <img src={`data:image/png;base64,${data.thumbnail_b64}`} alt={data.project_name} className="w-full h-full object-contain" />
          ) : (
            <div className="w-full h-full flex items-center justify-center text-farm-600">No preview</div>
          )}
        </div>
        
        {/* Details */}
        <div className="flex-1 p-4 md:p-6">
          <div className="flex items-center gap-2 mb-3">
            <div className="w-6 h-6 rounded-full bg-green-600 flex items-center justify-center flex-shrink-0">
              <Check size={14} className="text-white" />
            </div>
            <span className="text-green-400 font-medium text-sm">
                  {data?.is_new_model === false ? 'Added as variant to existing model' : 'Added to Model Library'}
                </span>
                {data?.printer_model && data.printer_model !== 'Unknown' && (
                  <span className="ml-2 px-2 py-0.5 bg-blue-500/20 text-blue-300 rounded text-xs">
                    {data.printer_model}
                  </span>
                )}
          </div>
          
          <h2 className="text-xl md:text-2xl font-display font-bold mb-4">{data.project_name}</h2>
          
          <div className="grid grid-cols-2 gap-3 md:gap-4 mb-4">
            <div className="flex items-center gap-2 md:gap-3">
              <Clock size={18} className="text-farm-500 flex-shrink-0" />
              <div>
                <p className="text-xs text-farm-500">Print Time</p>
                <p className="font-medium text-sm">{data.print_time_formatted || 'Unknown'}</p>
              </div>
            </div>
            <div className="flex items-center gap-2 md:gap-3">
              <Scale size={18} className="text-farm-500 flex-shrink-0" />
              <div>
                <p className="text-xs text-farm-500">Weight</p>
                <p className="font-medium text-sm">{data.total_weight_grams}g</p>
              </div>
            </div>
            <div className="flex items-center gap-2 md:gap-3">
              <Layers size={18} className="text-farm-500 flex-shrink-0" />
              <div>
                <p className="text-xs text-farm-500">Layers</p>
                <p className="font-medium text-sm">{data.layer_count}</p>
              </div>
            </div>
            <div className="flex items-center gap-2 md:gap-3">
              <Printer size={18} className="text-farm-500 flex-shrink-0" />
              <div>
                <p className="text-xs text-farm-500">Sliced For</p>
                <p className="font-medium text-sm">{data.printer_model || 'Unknown'}</p>
              </div>
            </div>
          </div>
          
          {data.filaments?.length > 0 && (
            <div className="mb-4">
              <p className="text-sm text-farm-500 mb-2">Filaments</p>
              <div className="flex flex-wrap gap-2">
                {data.filaments.map((f, i) => (<FilamentBadge key={i} filament={f} />))}
              </div>
            </div>
          )}

          {data.objects?.length > 0 && (
            <div className="mb-4">
              <div className="flex items-center justify-between mb-2">
                <p className="text-sm text-farm-500">Objects on Plate</p>
                <p className="text-xs text-farm-400">
                  Sellable pieces: <span className="text-green-400 font-medium">{data.objects.filter(o => o.checked).length}</span>
                </p>
              </div>
              <div className="bg-farm-800/50 rounded-lg p-2 max-h-32 overflow-y-auto space-y-1">
                {data.objects.map((obj, i) => (
                  <label key={i} className="flex items-center gap-2 p-1.5 hover:bg-farm-700/50 rounded cursor-pointer">
                    <input 
                      type="checkbox" 
                      checked={obj.checked}
                      onChange={() => {
                        const newObjects = [...data.objects]
                        newObjects[i] = { ...newObjects[i], checked: !newObjects[i].checked }
                        onUpdateObjects?.(newObjects)
                      }}
                      className="w-4 h-4 rounded border-farm-600 bg-farm-700 text-green-500 focus:ring-green-500"
                    />
                    <span className={`text-sm truncate ${obj.is_wipe_tower ? 'text-farm-500 italic' : ''}`}>
                      {obj.name}
                      {obj.is_wipe_tower && <span className="ml-2 text-xs text-amber-500">(wipe tower)</span>}
                    </span>
                  </label>
                ))}
              </div>
            </div>
          )}
          
          {!data.is_sliced && (
            <div className="bg-amber-900/30 border border-amber-700 rounded-lg p-3 mb-4">
              <p className="text-amber-400 text-sm">This file is not sliced. Print time and filament usage are estimates.</p>
            </div>
          )}
        </div>
      </div>
      
      <div className="flex justify-end gap-3 p-3 md:p-4 bg-farm-950 border-t border-farm-800">
        <button onClick={onUploadAnother} className="flex items-center gap-2 px-4 py-2 rounded-lg border border-farm-700 hover:bg-farm-800 transition-colors text-sm">
          <UploadIcon size={16} /> Upload Another
        </button>
        {data.model_id && (
          <button
            onClick={() => quickPrintMut.mutate()}
            disabled={quickPrintMut.isPending || quickPrinted}
            className="flex items-center gap-2 px-4 py-2 rounded-lg bg-amber-600 hover:bg-amber-500 disabled:opacity-50 text-white font-medium transition-colors text-sm"
          >
            <Zap size={16} />
            {quickPrinted ? 'Queued!' : quickPrintMut.isPending ? 'Queuing...' : 'Quick Print'}
          </button>
        )}
        <button onClick={onScheduleNow} className="flex items-center gap-2 px-4 py-2 rounded-lg bg-green-600 hover:bg-green-500 text-white font-medium transition-colors text-sm">
          <Calendar size={16} /> {submitForApproval ? 'Submit for Approval' : 'Schedule Now'}
        </button>
        <button onClick={onViewLibrary} className="flex items-center gap-2 px-4 md:px-6 py-2 rounded-lg bg-print-600 hover:bg-print-500 text-white font-medium transition-colors text-sm">
          View in Library <ArrowRight size={16} />
        </button>
      </div>
    </div>
  )
}

function RecentUploads() {
  const queryClient = useQueryClient()
  const deleteMutation = useMutation({ mutationFn: (id) => printFiles.delete(id), onSuccess: () => queryClient.invalidateQueries(["print-files"]) })
  const { data: files } = useQuery({ queryKey: ['print-files'], queryFn: () => printFiles.list() })
  
  if (!files || files.length === 0) return null
  
  return (
    <div className="mt-6 md:mt-8">
      <h3 className="text-base md:text-lg font-display font-semibold mb-4">Recent Uploads</h3>
      <div className="space-y-2">
        {files.slice(0, 5).map(f => (
          <div key={f.id} className="bg-farm-900 rounded-lg p-3 md:p-4 flex items-center justify-between gap-3">
            <div className="flex items-center gap-3 md:gap-4 min-w-0">
              {f.thumbnail_b64 && (
                <img src={`data:image/png;base64,${f.thumbnail_b64}`} alt={f.project_name} className="w-10 h-10 md:w-12 md:h-12 rounded object-contain bg-farm-950 flex-shrink-0" />
              )}
              <div className="min-w-0">
                <p className="font-medium text-sm truncate">{f.project_name}</p>
                <p className="text-xs text-farm-500">{f.print_time_formatted} • {f.total_weight_grams}g</p>
              </div>
            </div>
            <div className="flex items-center gap-2 flex-shrink-0">
              {f.job_id ? (
                <span className="text-xs bg-green-900/50 text-green-400 px-2 py-1 rounded">Scheduled</span>
              ) : (
                <span className="text-xs bg-print-900/50 text-print-400 px-2 py-1 rounded hidden sm:inline">In Library</span>
              )}
              <button onClick={() => deleteMutation.mutate(f.id)} className="p-1 text-farm-500 hover:text-red-400 transition-colors" title="Delete"><Trash2 size={16} /></button>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

export default function Upload() {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [uploadedFile, setUploadedFile] = useState(null)

  const { data: approvalSetting } = useQuery({
    queryKey: ['approval-setting'],
    queryFn: getApprovalSetting,
  })
  const approvalEnabled = approvalSetting?.require_job_approval || false
  // Check user role from localStorage token
  const userRole = (() => {
    try {
      const token = localStorage.getItem('token')
      if (!token) return null
      const payload = JSON.parse(atob(token.split('.')[1]))
      return payload.role
    } catch { return null }
  })()
  const showSubmitForApproval = approvalEnabled && userRole === 'viewer'
  
  const uploadMutation = useMutation({
    mutationFn: printFiles.upload,
    onSuccess: (data) => { setUploadedFile(data); queryClient.invalidateQueries(['print-files']); queryClient.invalidateQueries(['models']) }
  })
  
  const handleFileSelect = (file) => uploadMutation.mutate(file)
  
  return (
    <div className="p-4 md:p-8">
      <div className="mb-6 md:mb-8">
        <h1 className="text-2xl md:text-3xl font-display font-bold">Upload Print File</h1>
        <p className="text-farm-500 text-sm mt-1">Upload .3mf files to add to your model library</p>
      </div>
      
      {uploadedFile ? (
        <UploadSuccess 
          data={uploadedFile} 
          onUploadAnother={() => setUploadedFile(null)}
          onViewLibrary={() => navigate('/models')}
          onUpdateObjects={(newObjects) => {
            setUploadedFile(prev => ({ ...prev, objects: newObjects }))
            const checkedCount = newObjects.filter(o => o.checked).length
            if (uploadedFile?.model_id) {
              fetch(`/api/models/${uploadedFile.model_id}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json', 'X-API-Key': localStorage.getItem('api_key') || '' },
                body: JSON.stringify({ quantity_per_bed: checkedCount })
              }).catch(e => console.error('Failed to save quantity_per_bed:', e))
            }
          }}
          onScheduleNow={() => navigate(`/models?schedule=${uploadedFile.model_id}`)}
          submitForApproval={showSubmitForApproval}
        />
      ) : (
        <>
          <DropZone onFileSelect={handleFileSelect} isUploading={uploadMutation.isPending} />
          {uploadMutation.isError && (
            <div className="mt-4 bg-red-900/30 border border-red-700 rounded-lg p-4">
              <p className="text-red-400 text-sm">Failed to upload: {uploadMutation.error?.message || 'Unknown error'}</p>
            </div>
          )}
          <RecentUploads />
        </>
      )}
    </div>
  )
}
