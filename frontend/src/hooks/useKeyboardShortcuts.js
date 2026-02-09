import { useState, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'

/**
 * App-wide keyboard shortcuts.
 * 
 * Navigation (press g then letter within 1s):
 *   g d → Dashboard
 *   g j → Jobs
 *   g p → Printers
 *   g c → Cameras
 *   g u → Upload
 *   g m → Models
 *   g s → Spools
 *   g a → Analytics
 *   g o → Orders
 *   g t → Timeline
 *   g l → Alerts
 *   g x → Settings
 * 
 * Actions:
 *   Cmd/Ctrl+K → Global search (handled by GlobalSearch.jsx)
 *   ?          → Show keyboard shortcut help
 *   Escape     → Close help modal
 *   f          → Control Room mode (handled by Cameras.jsx)
 */
export default function useKeyboardShortcuts() {
  const navigate = useNavigate()
  const [showHelp, setShowHelp] = useState(false)
  const [gPrefix, setGPrefix] = useState(false)

  const handleKeyDown = useCallback((e) => {
    // Don't trigger in input/textarea/select elements
    const tag = e.target.tagName
    const editable = e.target.isContentEditable
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || editable) return

    // Don't trigger with modifier keys (except for Cmd+K which GlobalSearch handles)
    if (e.metaKey || e.ctrlKey || e.altKey) return

    const key = e.key.toLowerCase()

    // ? → show help
    if (e.key === '?') {
      e.preventDefault()
      setShowHelp(prev => !prev)
      return
    }

    // Escape → close help
    if (e.key === 'Escape') {
      setShowHelp(false)
      setGPrefix(false)
      return
    }

    // g prefix for navigation
    if (key === 'g' && !gPrefix) {
      setGPrefix(true)
      setTimeout(() => setGPrefix(false), 1000)
      return
    }

    // Navigation after g prefix
    if (gPrefix) {
      setGPrefix(false)
      const routes = {
        d: '/',
        j: '/jobs',
        p: '/printers',
        c: '/cameras',
        u: '/upload',
        m: '/models',
        s: '/spools',
        a: '/analytics',
        o: '/orders',
        t: '/timeline',
        l: '/alerts',
        x: '/settings',
        k: '/calculator',
        w: '/maintenance',
      }
      if (routes[key]) {
        e.preventDefault()
        navigate(routes[key])
        return
      }
    }
  }, [gPrefix, navigate])

  useEffect(() => {
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [handleKeyDown])

  return { showHelp, setShowHelp }
}
