import { Modal } from '../ui'

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
    { keys: ['n'], action: 'New item (Upload on Jobs/Models, context action on Spools/Orders)' },
  ]},
]

export default function KeyboardShortcutsModal({ onClose }) {
  return (
    <Modal isOpen={true} onClose={onClose} title="Keyboard Shortcuts" size="lg" mobileSheet={false}>
      <div className="space-y-5">
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
                        <kbd className="inline-flex items-center justify-center min-w-[24px] px-1.5 py-0.5 text-xs font-mono bg-farm-800 border border-farm-600 rounded-lg text-farm-200">
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
      <div className="pt-3 border-t border-farm-800 text-center mt-4">
        <span className="text-xs text-farm-500">Press <kbd className="px-1 py-0.5 text-xs bg-farm-800 border border-farm-600 rounded-lg">?</kbd> to toggle this help</span>
      </div>
    </Modal>
  )
}
