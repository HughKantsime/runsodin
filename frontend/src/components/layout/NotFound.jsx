import { Link } from 'react-router-dom'
import { FileQuestion } from 'lucide-react'

export default function NotFound() {
  return (
    <div className="flex flex-col items-center justify-center min-h-[50vh] p-8 text-center">
      <FileQuestion size={32} className="text-[var(--brand-text-muted)] mb-3" />
      <h2 className="text-lg font-semibold text-[var(--brand-text-primary)] mb-1">Page not found</h2>
      <p className="text-sm text-[var(--brand-text-muted)] mb-6">The page you're looking for doesn't exist.</p>
      <Link to="/" className="px-4 py-2 bg-print-600 hover:bg-print-500 text-white rounded-lg text-sm transition-colors">
        Back to Dashboard
      </Link>
    </div>
  )
}
