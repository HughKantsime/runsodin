import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Lock, User, AlertCircle } from 'lucide-react'

// NOTE: This page is not wired up yet. To enable:
// 1. Add route in App.jsx: <Route path="/login" element={<Login />} />
// 2. Add auth context provider
// 3. Add login API endpoint

export default function Login() {
  const navigate = useNavigate()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [isLoading, setIsLoading] = useState(false)

  const handleSubmit = async (e) => {
    e.preventDefault()
    setError('')
    setIsLoading(true)

    try {
      const response = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({ username, password })
      })

      if (!response.ok) {
        const data = await response.json()
        throw new Error(data.detail || 'Login failed')
      }

      const data = await response.json()
      
      // Store token
      localStorage.setItem('token', data.access_token)
      const payload = JSON.parse(atob(data.access_token.split('.')[1])); localStorage.setItem('user', JSON.stringify({ username: payload.sub, role: payload.role }))
      
      // Redirect to dashboard
      navigate('/')
    } catch (err) {
      setError(err.message)
    } finally {
      setIsLoading(false)
    }
  }

  return (
    <div className="min-h-screen bg-farm-950 flex items-center justify-center p-4">
      <div className="w-full max-w-md">
        <div className="bg-farm-900 rounded-xl border border-farm-800 p-8">
          {/* Logo */}
          <div className="text-center mb-8">
            <h1 className="text-3xl font-display font-bold text-print-400">PRINTFARM</h1>
            <p className="text-farm-500 mt-1">Scheduler</p>
          </div>

          {/* Error */}
          {error && (
            <div className="mb-6 p-4 bg-red-900/30 border border-red-700 rounded-lg flex items-center gap-3">
              <AlertCircle size={20} className="text-red-400" />
              <span className="text-red-400">{error}</span>
            </div>
          )}

          {/* Form */}
          <form onSubmit={handleSubmit} className="space-y-6">
            <div>
              <label className="block text-sm text-farm-400 mb-2">Username</label>
              <div className="relative">
                <User size={18} className="absolute left-3 top-1/2 -translate-y-1/2 text-farm-500" />
                <input
                  type="text"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  className="w-full bg-farm-800 border border-farm-700 rounded-lg py-3 pl-10 pr-4 focus:outline-none focus:border-print-500"
                  placeholder="Enter username"
                  required
                />
              </div>
            </div>

            <div>
              <label className="block text-sm text-farm-400 mb-2">Password</label>
              <div className="relative">
                <Lock size={18} className="absolute left-3 top-1/2 -translate-y-1/2 text-farm-500" />
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="w-full bg-farm-800 border border-farm-700 rounded-lg py-3 pl-10 pr-4 focus:outline-none focus:border-print-500"
                  placeholder="Enter password"
                  required
                />
              </div>
            </div>

            <button
              type="submit"
              disabled={isLoading}
              className="w-full bg-print-600 hover:bg-print-500 disabled:bg-farm-700 text-white font-medium py-3 rounded-lg transition-colors"
            >
              {isLoading ? 'Signing in...' : 'Sign In'}
            </button>
          </form>
        </div>

        <p className="text-center text-farm-600 text-sm mt-6">
          PrintFarm Scheduler v0.9.0
        </p>
      </div>
    </div>
  )
}
