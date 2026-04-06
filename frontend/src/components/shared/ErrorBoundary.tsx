import { Component, type ReactNode, type ErrorInfo } from 'react'
import { diagnostics } from '../../api'

interface ErrorBoundaryProps {
  children: ReactNode
}

interface ErrorBoundaryState {
  hasError: boolean
  error: Error | null
  showStack: boolean
  downloading: boolean
  downloadError: string | null
}

export default class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  constructor(props: ErrorBoundaryProps) {
    super(props)
    this.state = { hasError: false, error: null, showStack: false, downloading: false, downloadError: null }
  }

  static getDerivedStateFromError(error: Error): Partial<ErrorBoundaryState> {
    return { hasError: true, error }
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    console.error('ErrorBoundary caught:', error, errorInfo)
  }

  handleDownloadDiagnostics = async () => {
    this.setState({ downloading: true, downloadError: null })
    try {
      await diagnostics.download()
    } catch (err) {
      this.setState({ downloadError: err instanceof Error ? err.message : 'Failed to download diagnostics' })
    } finally {
      this.setState({ downloading: false })
    }
  }

  render() {
    if (this.state.hasError) {
      const { error, showStack, downloading, downloadError } = this.state

      return (
        <div style={{
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          minHeight: '50vh',
          padding: '2rem',
          textAlign: 'center',
          fontFamily: 'var(--font-sans, -apple-system, BlinkMacSystemFont, sans-serif)',
          color: 'var(--brand-text-primary, #e0e0e0)',
        }}>
          {/* Warning icon — inline SVG, no component deps */}
          <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="var(--brand-text-muted, #888)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ marginBottom: '1rem' }}>
            <path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z" />
            <line x1="12" y1="9" x2="12" y2="13" />
            <line x1="12" y1="17" x2="12.01" y2="17" />
          </svg>

          <h2 style={{ fontSize: '1.25rem', fontWeight: 600, marginBottom: '0.5rem' }}>
            Something went wrong
          </h2>

          <p style={{ fontSize: '0.875rem', color: 'var(--brand-text-muted, #888)', marginBottom: '0.5rem', maxWidth: '32rem' }}>
            {error?.message || 'An unexpected error occurred.'}
          </p>

          {/* Collapsible stack trace */}
          <button
            onClick={() => this.setState({ showStack: !showStack })}
            style={{
              background: 'none',
              border: 'none',
              color: 'var(--brand-text-muted, #888)',
              cursor: 'pointer',
              fontSize: '0.75rem',
              textDecoration: 'underline',
              marginBottom: '0.5rem',
            }}
          >
            {showStack ? 'Hide' : 'Show'} stack trace
          </button>

          {showStack && error?.stack && (
            <pre style={{
              textAlign: 'left',
              fontSize: '0.7rem',
              background: 'var(--brand-input-bg, #1a1a1a)',
              border: '1px solid var(--brand-card-border, #333)',
              borderRadius: '0.375rem',
              padding: '0.75rem',
              maxWidth: '40rem',
              width: '100%',
              overflow: 'auto',
              maxHeight: '200px',
              marginBottom: '1rem',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
            }}>
              {error.stack}
            </pre>
          )}

          {/* Action buttons */}
          <div style={{ display: 'flex', gap: '0.75rem', flexWrap: 'wrap', justifyContent: 'center', marginTop: '1rem' }}>
            <button
              onClick={() => window.location.reload()}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '0.5rem',
                padding: '0.5rem 1rem',
                border: '1px solid var(--brand-card-border, #333)',
                background: 'var(--brand-card-bg, #222)',
                color: 'var(--brand-text-secondary, #ccc)',
                borderRadius: '0.375rem',
                fontSize: '0.875rem',
                cursor: 'pointer',
              }}
            >
              Reload
            </button>

            <button
              onClick={this.handleDownloadDiagnostics}
              disabled={downloading}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '0.5rem',
                padding: '0.5rem 1rem',
                border: '1px solid var(--brand-card-border, #333)',
                background: 'var(--brand-card-bg, #222)',
                color: 'var(--brand-text-secondary, #ccc)',
                borderRadius: '0.375rem',
                fontSize: '0.875rem',
                cursor: downloading ? 'wait' : 'pointer',
                opacity: downloading ? 0.6 : 1,
              }}
            >
              {downloading ? 'Downloading...' : 'Download Diagnostics'}
            </button>

            <a
              href="https://github.com/HughKantsime/runsodin/issues"
              target="_blank"
              rel="noopener noreferrer"
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '0.5rem',
                padding: '0.5rem 1rem',
                border: '1px solid var(--brand-card-border, #333)',
                background: 'var(--brand-card-bg, #222)',
                color: 'var(--brand-text-secondary, #ccc)',
                borderRadius: '0.375rem',
                fontSize: '0.875rem',
                textDecoration: 'none',
              }}
            >
              Report Issue
            </a>
          </div>

          {downloadError && (
            <p style={{ fontSize: '0.75rem', color: '#f87171', marginTop: '0.75rem' }}>
              {downloadError}
            </p>
          )}
        </div>
      )
    }

    return this.props.children
  }
}
