import { Link } from 'react-router-dom'

export default function NotFound() {
  return (
    <div className="flex flex-col items-center justify-center min-h-[50vh] p-8 text-center">
      <h2 className="text-4xl font-bold text-farm-100 mb-2">404</h2>
      <p className="text-sm text-farm-400 mb-6">Page not found</p>
      <Link to="/" className="px-4 py-2 bg-print-600 hover:bg-print-500 text-white rounded-lg text-sm transition-colors">
        Back to Dashboard
      </Link>
    </div>
  )
}
