import { useState, useEffect, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { Bell } from 'lucide-react'
import { alerts as alertsApi } from '../api'

const SEVERITY_STYLES = {
  critical: { dot: 'bg-red-500', text: 'text-red-400', icon: '\u{1F534}' },
  warning: { dot: 'bg-amber-500', text: 'text-amber-400', icon: '\u{1F7E1}' },
  info: { dot: 'bg-green-500', text: 'text-green-400', icon: '\u{1F7E2}' },
}

function timeAgo(dateStr) {
  const now = new Date()
  const date = new Date(dateStr)
  const seconds = Math.floor((now - date) / 1000)
  if (seconds < 60) return 'just now'
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}

export default function AlertBell() {
  const [unreadCount, setUnreadCount] = useState(0)
  const [recentAlerts, setRecentAlerts] = useState([])
  const [isOpen, setIsOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const dropdownRef = useRef(null)
  const navigate = useNavigate()

  useEffect(() => {
    const fetchCount = async () => {
      try {
        const data = await alertsApi.unreadCount()
        setUnreadCount(data.unread_count)
      } catch (err) {}
    }
    fetchCount()
    const interval = setInterval(fetchCount, 10000)
    return () => clearInterval(interval)
  }, [])

  useEffect(() => {
    if (isOpen) {
      setLoading(true)
      alertsApi.list({ limit: 10 })
        .then(data => setRecentAlerts(data))
        .catch(() => {})
        .finally(() => setLoading(false))
    }
  }, [isOpen])

  useEffect(() => {
    const handleClickOutside = (e) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target)) {
        setIsOpen(false)
      }
    }
    if (isOpen) document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [isOpen])

  const handleAlertClick = async (alert) => {
    try {
      if (!alert.is_read) {
        await alertsApi.markRead(alert.id)
        setUnreadCount(prev => Math.max(0, prev - 1))
        setRecentAlerts(prev => prev.map(a => a.id === alert.id ? { ...a, is_read: true } : a))
      }
    } catch (err) {}
    setIsOpen(false)
    if (alert.job_id) navigate('/jobs')
    else if (alert.spool_id) navigate('/spools')
    else if (alert.printer_id) navigate('/printers')
    else navigate('/alerts')
  }

  const handleMarkAllRead = async () => {
    try {
      await alertsApi.markAllRead()
      setUnreadCount(0)
      setRecentAlerts(prev => prev.map(a => ({ ...a, is_read: true })))
    } catch (err) {}
  }

  return (
    <div className="relative" ref={dropdownRef}>
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="relative p-2 rounded-lg transition-colors hover:bg-farm-800"
        style={{ color: 'var(--brand-text-secondary)' }}
      >
        <Bell size={20} />
        {unreadCount > 0 && (
          <span className="absolute -top-0.5 -right-0.5 min-w-[18px] h-[18px] flex items-center justify-center bg-red-500 text-white text-[10px] font-bold rounded-full px-1">
            {unreadCount > 99 ? '99+' : unreadCount}
          </span>
        )}
      </button>

      {isOpen && (
        <div
          className="absolute right-0 top-full mt-2 w-80 rounded-lg border shadow-xl z-50 overflow-hidden"
          style={{
            backgroundColor: 'var(--brand-card-bg)',
            borderColor: 'var(--brand-sidebar-border)',
          }}
        >
          <div className="flex items-center justify-between px-4 py-3" style={{ borderBottom: '1px solid var(--brand-sidebar-border)' }}>
            <span className="text-sm font-semibold" style={{ color: 'var(--brand-text-primary)' }}>Notifications</span>
            {unreadCount > 0 && (
              <button
                onClick={handleMarkAllRead}
                className="text-xs transition-colors"
                style={{ color: 'var(--brand-accent)' }}
              >
                Mark all read
              </button>
            )}
          </div>

          <div className="max-h-80 overflow-y-auto">
            {loading ? (
              <div className="p-4 text-center text-sm" style={{ color: 'var(--brand-text-muted)' }}>Loading...</div>
            ) : recentAlerts.length === 0 ? (
              <div className="p-4 text-center text-sm" style={{ color: 'var(--brand-text-muted)' }}>No notifications</div>
            ) : (
              recentAlerts.map(alert => {
                const sev = SEVERITY_STYLES[alert.severity] || SEVERITY_STYLES.info
                return (
                  <button
                    key={alert.id}
                    onClick={() => handleAlertClick(alert)}
                    className="w-full text-left px-4 py-3 transition-colors hover:bg-farm-800/50"
                    style={{
                      borderBottom: '1px solid var(--brand-sidebar-border)',
                      opacity: alert.is_read ? 0.6 : 1,
                    }}
                  >
                    <div className="flex gap-2">
                      <span className="text-sm flex-shrink-0 mt-0.5">{sev.icon}</span>
                      <div className="min-w-0 flex-1">
                        <div className={`text-sm font-medium truncate ${!alert.is_read ? 'text-white' : ''}`} style={alert.is_read ? { color: 'var(--brand-text-secondary)' } : {}}>
                          {alert.title}
                        </div>
                        {alert.message && (
                          <div className="text-xs mt-0.5 truncate" style={{ color: 'var(--brand-text-muted)' }}>
                            {alert.message}
                          </div>
                        )}
                        <div className="text-[10px] mt-1" style={{ color: 'var(--brand-text-muted)' }}>
                          {timeAgo(alert.created_at)}
                        </div>
                      </div>
                      {!alert.is_read && <span className="w-2 h-2 rounded-full bg-blue-500 flex-shrink-0 mt-2" />}
                    </div>
                  </button>
                )
              })
            )}
          </div>

          <div className="px-4 py-2.5" style={{ borderTop: '1px solid var(--brand-sidebar-border)' }}>
            <button
              onClick={() => { setIsOpen(false); navigate('/alerts') }}
              className="text-xs font-medium transition-colors w-full text-center"
              style={{ color: 'var(--brand-accent)' }}
            >
              View all alerts
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
