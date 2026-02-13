import { useEffect } from 'react'
import { X } from 'lucide-react'

export default function DetailDrawer({ open, onClose, title, children }) {
  useEffect(() => {
    if (!open) return
    const handleKeyDown = (e) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [open, onClose])

  return (
    <>
      {/* Backdrop */}
      <div
        className={`fixed inset-0 bg-black/50 z-50 transition-opacity duration-300 ${open ? 'opacity-100' : 'opacity-0 pointer-events-none'}`}
        onClick={onClose}
      />

      {/* Panel */}
      <div
        className={`fixed inset-y-0 right-0 w-full sm:w-[420px] bg-farm-950 border-l border-farm-800 z-50 flex flex-col transition-transform duration-300 ${open ? 'translate-x-0' : 'translate-x-full'}`}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-farm-800">
          <h2 className="text-lg font-semibold text-farm-100 truncate">{title}</h2>
          <button
            onClick={onClose}
            className="p-1.5 hover:bg-farm-800 rounded-lg text-farm-400 hover:text-farm-200"
          >
            <X size={18} />
          </button>
        </div>

        {/* Body */}
        <div className="overflow-y-auto flex-1 p-4">
          {children}
        </div>
      </div>
    </>
  )
}
