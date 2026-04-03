/**
 * NotificationPreferences — per-event-type notification configuration.
 *
 * Users can choose which events trigger notifications and through which channels.
 * Saves to user preferences via API.
 */

import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Bell, BellOff, Mail, MessageSquare, Webhook,
  AlertTriangle, CheckCircle, XCircle, Printer, Eye,
} from 'lucide-react'
import { Button } from '../ui'
import { system } from '../../api'
import toast from 'react-hot-toast'

interface NotificationPref {
  eventType: string
  label: string
  description: string
  icon: React.ReactNode
  browser: boolean
  email: boolean
  webhook: boolean
}

const DEFAULT_PREFS: NotificationPref[] = [
  {
    eventType: 'print_complete',
    label: 'Print Completed',
    description: 'A print job finished successfully',
    icon: <CheckCircle size={16} className="text-green-500" />,
    browser: true, email: false, webhook: true,
  },
  {
    eventType: 'print_failed',
    label: 'Print Failed',
    description: 'A print job failed or was cancelled',
    icon: <XCircle size={16} className="text-red-500" />,
    browser: true, email: true, webhook: true,
  },
  {
    eventType: 'ai_detection',
    label: 'Vigil AI Detection',
    description: 'AI detected a print quality issue (spaghetti, detachment, etc.)',
    icon: <Eye size={16} className="text-amber-500" />,
    browser: true, email: true, webhook: true,
  },
  {
    eventType: 'printer_offline',
    label: 'Printer Offline',
    description: 'A printer lost connection',
    icon: <Printer size={16} className="text-red-400" />,
    browser: true, email: false, webhook: true,
  },
  {
    eventType: 'printer_error',
    label: 'Printer Error (HMS)',
    description: 'A printer reported a hardware/firmware error',
    icon: <AlertTriangle size={16} className="text-amber-400" />,
    browser: true, email: false, webhook: true,
  },
  {
    eventType: 'low_filament',
    label: 'Low Filament',
    description: 'A spool dropped below the low-stock threshold',
    icon: <AlertTriangle size={16} className="text-orange-400" />,
    browser: false, email: false, webhook: false,
  },
  {
    eventType: 'job_queued',
    label: 'Job Queued',
    description: 'A new job was added to the queue',
    icon: <Bell size={16} className="text-blue-400" />,
    browser: false, email: false, webhook: false,
  },
]

interface ChannelToggleProps {
  enabled: boolean
  onChange: (v: boolean) => void
  icon: React.ReactNode
  label: string
}

function ChannelToggle({ enabled, onChange, icon, label }: ChannelToggleProps) {
  return (
    <button
      type="button"
      onClick={() => onChange(!enabled)}
      className={`flex items-center justify-center w-8 h-8 rounded transition-colors ${
        enabled
          ? 'bg-accent/20 text-accent'
          : 'bg-muted/50 text-muted-foreground hover:bg-muted'
      }`}
      title={`${label}: ${enabled ? 'On' : 'Off'}`}
      aria-label={`${label} notifications ${enabled ? 'enabled' : 'disabled'}`}
    >
      {icon}
    </button>
  )
}

export default function NotificationPreferences() {
  const [prefs, setPrefs] = useState<NotificationPref[]>(DEFAULT_PREFS)
  const queryClient = useQueryClient()

  // Load saved preferences
  const { data: savedPrefs } = useQuery({
    queryKey: ['notification-prefs'],
    queryFn: async () => {
      try {
        const res = await fetch('/api/settings/notification-preferences', { credentials: 'include' })
        if (res.ok) return res.json()
      } catch {}
      return null
    },
  })

  useEffect(() => {
    if (savedPrefs?.preferences) {
      setPrefs(prev => prev.map(p => {
        const saved = savedPrefs.preferences[p.eventType]
        if (saved) return { ...p, ...saved }
        return p
      }))
    }
  }, [savedPrefs])

  const saveMutation = useMutation({
    mutationFn: async (newPrefs: NotificationPref[]) => {
      const payload: Record<string, { browser: boolean; email: boolean; webhook: boolean }> = {}
      for (const p of newPrefs) {
        payload[p.eventType] = { browser: p.browser, email: p.email, webhook: p.webhook }
      }
      const res = await fetch('/api/settings/notification-preferences', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ preferences: payload }),
      })
      if (!res.ok) throw new Error('Failed to save')
      return res.json()
    },
    onSuccess: () => {
      toast.success('Notification preferences saved')
      queryClient.invalidateQueries({ queryKey: ['notification-prefs'] })
    },
    onError: () => toast.error('Failed to save preferences'),
  })

  const updatePref = (idx: number, field: 'browser' | 'email' | 'webhook', value: boolean) => {
    const updated = [...prefs]
    updated[idx] = { ...updated[idx], [field]: value }
    setPrefs(updated)
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-sm font-semibold text-foreground">Notification Preferences</h3>
          <p className="text-xs text-muted-foreground mt-0.5">Choose which events notify you and how</p>
        </div>
        <Button
          size="sm"
          variant="primary"
          onClick={() => saveMutation.mutate(prefs)}
          disabled={saveMutation.isPending}
        >
          Save
        </Button>
      </div>

      {/* Channel headers */}
      <div className="grid grid-cols-[1fr_auto_auto_auto] gap-2 items-center px-3 text-xs text-muted-foreground">
        <span>Event</span>
        <span className="w-8 text-center" title="Browser notifications">
          <Bell size={12} className="mx-auto" />
        </span>
        <span className="w-8 text-center" title="Email notifications">
          <Mail size={12} className="mx-auto" />
        </span>
        <span className="w-8 text-center" title="Webhook delivery">
          <Webhook size={12} className="mx-auto" />
        </span>
      </div>

      {/* Preference rows */}
      <div className="space-y-1">
        {prefs.map((pref, idx) => (
          <div
            key={pref.eventType}
            className="grid grid-cols-[1fr_auto_auto_auto] gap-2 items-center px-3 py-2 rounded-lg hover:bg-muted/30 transition-colors"
          >
            <div className="flex items-center gap-2 min-w-0">
              {pref.icon}
              <div className="min-w-0">
                <div className="text-sm font-medium text-foreground truncate">{pref.label}</div>
                <div className="text-xs text-muted-foreground truncate">{pref.description}</div>
              </div>
            </div>
            <ChannelToggle
              enabled={pref.browser}
              onChange={(v) => updatePref(idx, 'browser', v)}
              icon={pref.browser ? <Bell size={14} /> : <BellOff size={14} />}
              label="Browser"
            />
            <ChannelToggle
              enabled={pref.email}
              onChange={(v) => updatePref(idx, 'email', v)}
              icon={<Mail size={14} />}
              label="Email"
            />
            <ChannelToggle
              enabled={pref.webhook}
              onChange={(v) => updatePref(idx, 'webhook', v)}
              icon={<Webhook size={14} />}
              label="Webhook"
            />
          </div>
        ))}
      </div>
    </div>
  )
}
