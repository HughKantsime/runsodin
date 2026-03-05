import { Component } from 'react'
import { AlertTriangle, RefreshCw } from 'lucide-react'

export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { hasError: false, error: null }
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error }
  }

  componentDidCatch(error, errorInfo) {
    console.error('ErrorBoundary caught:', error, errorInfo)
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="flex flex-col items-center justify-center min-h-[50vh] p-8 text-center">
          <AlertTriangle size={48} className="text-[var(--brand-text-muted)] mb-4" />
          <h2 className="text-xl font-semibold text-[var(--brand-text-primary)] mb-2">Something went wrong</h2>
          <p className="text-sm text-[var(--brand-text-muted)] mb-6 max-w-md">
            An unexpected error occurred. Try reloading the page.
          </p>
          <button
            onClick={() => window.location.reload()}
            className="flex items-center gap-2 px-4 py-2 border border-[var(--brand-card-border)] hover:bg-[var(--brand-card-border)] text-[var(--brand-text-secondary)] rounded-md text-sm transition-colors"
          >
            <RefreshCw size={16} />
            Reload
          </button>
        </div>
      )
    }

    return this.props.children
  }
}
