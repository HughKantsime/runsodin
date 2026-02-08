import { X } from 'lucide-react'

const shortcuts = [
  { category: 'Navigation (press g then letter)', items: [
    { keys: ['g', 'd'], action: 'Dashboard' },
    { keys: ['g', 'j'], action: 'Jobs' },
    { keys: ['g', 'p'], action: 'Printers' },
    { keys: ['g', 'c'], action: 'Cameras' },
    { keys: ['g', 'u'], action: 'Upload' },
    { keys: ['g', 'm'], action: 'Models' },
    { keys: ['g', 's'], action: 'Spools' },
    { keys: ['g', 'o'], action: 'Orders' },
    { keys: ['g', 'a'], action: 'Analytics' },
    { keys: ['g', 't'], action: 'Timeline' },
    { keys: ['g', 'l'], action: 'Alerts' },
    { keys: ['g', 'x'], action: 'Settings' },
  ]},
  { category: 'Actions', items: [
    { keys: ['⌘', 'K'], action: 'Global search' },
    { keys: ['?'], action: 'Show this help' },
    { keys: ['Esc'], action: 'Close modal / cancel' },
    { keys: ['f'], action: 'Control Room mode (on Cameras page)' },
  ]},
]

export default function KeyboardShortcutsModal({ onClose }) {
  return (
    <div className="fixed inset-0 z-[200] flex items-center justify-center" onClick={onClose}>
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" />
      <div
        className="relative bg-farm-950 border border-farm-700 rounded-lg shadow-2xl w-full max-w-lg mx-4 max-h-[80vh] overflow-y-auto"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-5 py-4 border-b border-farm-800">
          <h2 className="text-lg font-semibold text-farm-100">Keyboard Shortcuts</h2>
          <button onClick={onClose} className="p-1 text-farm-400 hover:text-farm-200 rounded">
            <X size={18} />
          </button>
        </div>
        <div className="px-5 py-4 space-y-5">
          {shortcuts.map(group => (
            <div key={group.category}>
              <h3 className="text-xs font-medium text-farm-400 uppercase tracking-wider mb-2">
                {group.category}
              </h3>
              <div className="space-y-1.5">
                {group.items.map(item => (
                  <div key={item.action} className="flex items-center justify-between py-1">
                    <span className="text-sm text-farm-300">{item.action}</span>
                    <div className="flex items-center gap-1">
                      {item.keys.map((key, i) => (
                        <span key={i}>
                          {i > 0 && <span className="text-farm-600 mx-0.5">+</span>}
                          <kbd className="inline-flex items-center justify-center min-w-[24px] px-1.5 py-0.5 text-xs font-mono bg-farm-800 border border-farm-600 rounded text-farm-200">
                            {key}
                          </kbd>
                        </span>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
        <div className="px-5 py-3 border-t border-farm-800 text-center">
          <span className="text-xs text-farm-500">Press <kbd className="px-1 py-0.5 text-xs bg-farm-800 border border-farm-600 rounded">?</kbd> to toggle this help</span>
        </div>
      </div>
    </div>
  )
}
