import { Component } from 'react'
import { AlertCircle, RefreshCw } from 'lucide-react'

function SectionErrorFallback({ onRetry }) {
  return (
    <div className="bg-farm-900/50 border border-farm-700 rounded-lg p-6 flex flex-col items-center justify-center text-center">
      <AlertCircle size={24} className="text-farm-500 mb-2" />
      <p className="text-sm text-farm-400 mb-3">This section couldn't load</p>
      <button
        onClick={onRetry}
        className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-farm-300 bg-farm-800 hover:bg-farm-700 border border-farm-600 rounded-lg transition-colors"
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
