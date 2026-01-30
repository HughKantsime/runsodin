import { useState, useCallback } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Upload as UploadIcon, FileUp, Clock, Scale, Layers, Check, X, Printer, Play } from 'lucide-react'
import clsx from 'clsx'

import { printFiles, printers } from '../api'

function DropZone({ onFileSelect, isUploading }) {
  const [isDragging, setIsDragging] = useState(false)
  
  const handleDrag = useCallback((e) => {
    e.preventDefault()
    e.stopPropagation()
  }, [])
  
  const handleDragIn = useCallback((e) => {
    e.preventDefault()
    e.stopPropagation()
    setIsDragging(true)
  }, [])
  
  const handleDragOut = useCallback((e) => {
    e.preventDefault()
    e.stopPropagation()
    setIsDragging(false)
  }, [])
  
  const handleDrop = useCallback((e) => {
    e.preventDefault()
    e.stopPropagation()
    setIsDragging(false)
    
    const files = e.dataTransfer?.files
    if (files && files.length > 0) {
      const file = files[0]
      if (file.name.endsWith('.3mf')) {
        onFileSelect(file)
      }
    }
  }, [onFileSelect])
  
  const handleFileInput = (e) => {
    const file = e.target.files?.[0]
    if (file) {
      onFileSelect(file)
    }
  }
  
  return (
    <div
      className={clsx(
        'border-2 border-dashed rounded-xl p-12 text-center transition-all',
        isDragging ? 'border-print-500 bg-print-900/20' : 'border-farm-700 hover:border-farm-500',
        isUploading && 'opacity-50 pointer-events-none'
      )}
      onDragEnter={handleDragIn}
      onDragLeave={handleDragOut}
      onDragOver={handleDrag}
      onDrop={handleDrop}
    >
      <input
        type="file"
        accept=".3mf"
        onChange={handleFileInput}
        className="hidden"
        id="file-upload"
        disabled={isUploading}
      />
      <label htmlFor="file-upload" className="cursor-pointer">
        <div className="flex flex-col items-center gap-4">
          <div className={clsx(
            'p-4 rounded-full',
            isDragging ? 'bg-print-600' : 'bg-farm-800'
          )}>
            <FileUp size={48} className={isDragging ? 'text-white' : 'text-farm-400'} />
          </div>
          <div>
            <p className="text-lg font-medium">
              {isUploading ? 'Uploading...' : 'Drop .3mf file here'}
            </p>
            <p className="text-farm-500 text-sm mt-1">
              or click to browse
            </p>
          </div>
        </div>
      </label>
    </div>
  )
}

function FilamentBadge({ filament }) {
  return (
    <div className="flex items-center gap-2 bg-farm-800 rounded-lg px-3 py-2">
      <div 
        className="w-4 h-4 rounded-full border border-farm-600"
        style={{ backgroundColor: filament.color }}
      />
      <span className="text-sm">{filament.type}</span>
      <span className="text-xs text-farm-500">{filament.used_grams}g</span>
    </div>
  )
}

function PrintPreview({ data, onSchedule, onCancel, isScheduling }) {
  const { data: printersList } = useQuery({ 
    queryKey: ['printers'], 
    queryFn: () => printers.list(true) 
  })
  
  const [selectedPrinter, setSelectedPrinter] = useState(null)
  
  // Find compatible printers (simplified - checks if printer has the right filament type loaded)
  const compatiblePrinters = printersList?.filter(p => {
    if (!data.filaments || data.filaments.length === 0) return true
    // For now, all active printers are "compatible" - could add filament matching later
    return p.is_active
  }) || []
  
  return (
    <div className="bg-farm-900 rounded-xl border border-farm-800 overflow-hidden">
      <div className="flex">
        {/* Thumbnail */}
        <div className="w-64 h-64 bg-farm-950 flex-shrink-0">
          {data.thumbnail_b64 ? (
            <img 
              src={`data:image/png;base64,${data.thumbnail_b64}`}
              alt={data.project_name}
              className="w-full h-full object-contain"
            />
          ) : (
            <div className="w-full h-full flex items-center justify-center text-farm-600">
              No preview
            </div>
          )}
        </div>
        
        {/* Details */}
        <div className="flex-1 p-6">
          <h2 className="text-2xl font-display font-bold mb-4">{data.project_name}</h2>
          
          <div className="grid grid-cols-2 gap-4 mb-6">
            <div className="flex items-center gap-3">
              <Clock size={20} className="text-farm-500" />
              <div>
                <p className="text-sm text-farm-500">Print Time</p>
                <p className="font-medium">{data.print_time_formatted || 'Unknown'}</p>
              </div>
            </div>
            
            <div className="flex items-center gap-3">
              <Scale size={20} className="text-farm-500" />
              <div>
                <p className="text-sm text-farm-500">Weight</p>
                <p className="font-medium">{data.total_weight_grams}g</p>
              </div>
            </div>
            
            <div className="flex items-center gap-3">
              <Layers size={20} className="text-farm-500" />
              <div>
                <p className="text-sm text-farm-500">Layers</p>
                <p className="font-medium">{data.layer_count}</p>
              </div>
            </div>
            
            <div className="flex items-center gap-3">
              <Printer size={20} className="text-farm-500" />
              <div>
                <p className="text-sm text-farm-500">Sliced For</p>
                <p className="font-medium">{data.printer_model || 'Unknown'}</p>
              </div>
            </div>
          </div>
          
          {/* Filaments */}
          <div className="mb-6">
            <p className="text-sm text-farm-500 mb-2">Filaments Required</p>
            <div className="flex flex-wrap gap-2">
              {data.filaments?.map((f, i) => (
                <FilamentBadge key={i} filament={f} />
              ))}
            </div>
          </div>
          
          {/* Printer Selection */}
          {!data.is_sliced ? (
            <div className="bg-amber-900/30 border border-amber-700 rounded-lg p-4 mb-4">
              <p className="text-amber-400 text-sm">
                This file is not sliced. Print time and filament usage are unknown.
              </p>
            </div>
          ) : (
            <div className="mb-4">
              <p className="text-sm text-farm-500 mb-2">Schedule on Printer</p>
              <div className="flex flex-wrap gap-2">
                {compatiblePrinters.map(p => (
                  <button
                    key={p.id}
                    onClick={() => setSelectedPrinter(p.id)}
                    className={clsx(
                      'px-4 py-2 rounded-lg border transition-colors',
                      selectedPrinter === p.id 
                        ? 'border-print-500 bg-print-900/30 text-print-400'
                        : 'border-farm-700 hover:border-farm-500'
                    )}
                  >
                    {p.name}
                  </button>
                ))}
                <button
                  onClick={() => setSelectedPrinter(null)}
                  className={clsx(
                    'px-4 py-2 rounded-lg border transition-colors',
                    selectedPrinter === null 
                      ? 'border-print-500 bg-print-900/30 text-print-400'
                      : 'border-farm-700 hover:border-farm-500'
                  )}
                >
                  Auto-assign
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
      
      {/* Actions */}
      <div className="flex justify-end gap-3 p-4 bg-farm-950 border-t border-farm-800">
        <button
          onClick={onCancel}
          disabled={isScheduling}
          className="flex items-center gap-2 px-4 py-2 rounded-lg border border-farm-700 hover:bg-farm-800 transition-colors"
        >
          <X size={18} />
          Cancel
        </button>
        <button
          onClick={() => onSchedule(selectedPrinter)}
          disabled={isScheduling || !data.is_sliced}
          className={clsx(
            'flex items-center gap-2 px-6 py-2 rounded-lg font-medium transition-colors',
            data.is_sliced 
              ? 'bg-print-600 hover:bg-print-500 text-white'
              : 'bg-farm-700 text-farm-500 cursor-not-allowed'
          )}
        >
          <Play size={18} />
          {isScheduling ? 'Scheduling...' : 'Schedule Print'}
        </button>
      </div>
    </div>
  )
}

function RecentUploads() {
  const { data: files } = useQuery({
    queryKey: ['print-files'],
    queryFn: () => printFiles.list()
  })
  
  if (!files || files.length === 0) return null
  
  return (
    <div className="mt-8">
      <h3 className="text-lg font-display font-semibold mb-4">Recent Uploads</h3>
      <div className="space-y-2">
        {files.slice(0, 5).map(f => (
          <div key={f.id} className="bg-farm-900 rounded-lg p-4 flex items-center justify-between">
            <div className="flex items-center gap-4">
              {f.thumbnail_b64 && (
                <img 
                  src={`data:image/png;base64,${f.thumbnail_b64}`}
                  alt={f.project_name}
                  className="w-12 h-12 rounded object-contain bg-farm-950"
                />
              )}
              <div>
                <p className="font-medium">{f.project_name}</p>
                <p className="text-sm text-farm-500">
                  {f.print_time_formatted} • {f.total_weight_grams}g
                </p>
              </div>
            </div>
            <div>
              {f.job_id ? (
                <span className="text-xs bg-green-900/50 text-green-400 px-2 py-1 rounded">
                  Scheduled
                </span>
              ) : (
                <span className="text-xs bg-farm-800 text-farm-400 px-2 py-1 rounded">
                  Not scheduled
                </span>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

export default function Upload() {
  const queryClient = useQueryClient()
  const [uploadedFile, setUploadedFile] = useState(null)
  
  const uploadMutation = useMutation({
    mutationFn: printFiles.upload,
    onSuccess: (data) => {
      setUploadedFile(data)
      queryClient.invalidateQueries(['print-files'])
    }
  })
  
  const scheduleMutation = useMutation({
    mutationFn: ({ fileId, printerId }) => printFiles.schedule(fileId, printerId),
    onSuccess: () => {
      setUploadedFile(null)
      queryClient.invalidateQueries(['print-files'])
      queryClient.invalidateQueries(['jobs'])
    }
  })
  
  const handleFileSelect = (file) => {
    uploadMutation.mutate(file)
  }
  
  const handleSchedule = (printerId) => {
    if (uploadedFile) {
      scheduleMutation.mutate({ fileId: uploadedFile.id, printerId })
    }
  }
  
  const handleCancel = () => {
    setUploadedFile(null)
  }
  
  return (
    <div className="p-8">
      <div className="mb-8">
        <h1 className="text-3xl font-display font-bold">Upload Print File</h1>
        <p className="text-farm-500 mt-1">Upload a sliced .3mf file to schedule a print</p>
      </div>
      
      {uploadedFile ? (
        <PrintPreview 
          data={uploadedFile}
          onSchedule={handleSchedule}
          onCancel={handleCancel}
          isScheduling={scheduleMutation.isPending}
        />
      ) : (
        <>
          <DropZone 
            onFileSelect={handleFileSelect}
            isUploading={uploadMutation.isPending}
          />
          
          {uploadMutation.isError && (
            <div className="mt-4 bg-red-900/30 border border-red-700 rounded-lg p-4">
              <p className="text-red-400">
                Failed to upload: {uploadMutation.error?.message || 'Unknown error'}
              </p>
            </div>
          )}
          
          <RecentUploads />
        </>
      )}
    </div>
  )
}
