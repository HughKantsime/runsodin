import { Component } from 'react'
import { AlertCircle, RefreshCw } from 'lucide-react'

function SectionErrorFallback({ onRetry }) {
  return (
    <div className="bg-[var(--brand-card-bg)]/50 border border-[var(--brand-card-border)] rounded-md p-6 flex flex-col items-center justify-center text-center">
      <AlertCircle size={24} className="text-[var(--brand-text-muted)] mb-2" />
      <p className="text-sm text-[var(--brand-text-secondary)] mb-3">This section couldn't load</p>
      <button
        onClick={onRetry}
        className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-[var(--brand-text-primary)] bg-[var(--brand-input-bg)] hover:bg-[var(--brand-card-bg)] border border-[var(--brand-card-border)] rounded-md transition-colors"
      >
        <RefreshCw size={12} />
        Retry
      </button>
    </div>
  )
}

export default class SectionErrorBoundary extends Component {
  state = { hasError: false }

  static getDerivedStateFromError() {
    return { hasError: true }
  }

  componentDidCatch(error, errorInfo) {
    console.error('SectionErrorBoundary caught:', error, errorInfo)
  }

  handleRetry = () => {
    this.setState({ hasError: false })
  }

  render() {
    if (this.state.hasError) {
      return <SectionErrorFallback onRetry={this.handleRetry} />
    }
    return this.props.children
  }
}
