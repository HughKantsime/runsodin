/**
 * OnboardingTour — guided walkthrough for new users.
 *
 * Shows after setup wizard completes, guides user through:
 * 1. Dashboard overview
 * 2. Add your first printer
 * 3. Explore the job queue
 * 4. Check camera feeds
 * 5. Inventory & filament tracking
 *
 * Dismissible, remembers completion via localStorage.
 * Re-accessible from Settings > Help > Restart Tour.
 */

import { useState, useEffect, useCallback } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import {
  Monitor, Printer, Calendar, Video, Package,
  ArrowRight, ArrowLeft, X, Sparkles, Check,
} from 'lucide-react'
import { Button } from '../ui'

interface TourStep {
  id: string
  title: string
  description: string
  icon: React.ReactNode
  targetRoute: string
  highlight?: string  // CSS selector to highlight
  action?: string     // CTA text
}

const TOUR_STEPS: TourStep[] = [
  {
    id: 'dashboard',
    title: 'Your Command Center',
    description: 'This is your dashboard. It shows printer status, active jobs, alerts, and quick stats at a glance. Everything updates in real-time via WebSocket — no refreshing needed.',
    icon: <Monitor size={24} />,
    targetRoute: '/',
    action: 'Next: Add a Printer',
  },
  {
    id: 'add-printer',
    title: 'Add Your First Printer',
    description: 'O.D.I.N. supports Bambu Lab (MQTT), Klipper/Moonraker, PrusaLink, and Elegoo printers. Click "Add Printer" and enter your printer\'s IP address — O.D.I.N. will auto-detect the protocol and model.',
    icon: <Printer size={24} />,
    targetRoute: '/printers',
    action: 'Next: Job Queue',
  },
  {
    id: 'jobs',
    title: 'Schedule & Manage Jobs',
    description: 'Upload .3mf files, create print jobs, and let the scheduler match them to the right printer based on filament color, machine capability, and availability. Drag and drop to reorder the queue.',
    icon: <Calendar size={24} />,
    targetRoute: '/jobs',
    action: 'Next: Camera Feeds',
  },
  {
    id: 'cameras',
    title: 'Live Camera Monitoring',
    description: 'Watch your printers in real-time via WebRTC. The control room shows all cameras in a grid. Vigil AI runs locally on your hardware to detect failures — spaghetti, detachment, and first-layer issues trigger auto-pause.',
    icon: <Video size={24} />,
    targetRoute: '/cameras',
    action: 'Next: Inventory',
  },
  {
    id: 'inventory',
    title: 'Track Every Spool',
    description: 'Manage your filament inventory with QR labels, AMS integration, and auto-deduction on print completion. Set low-stock alerts so you never run out mid-print. Integrates with Spoolman if you use it.',
    icon: <Package size={24} />,
    targetRoute: '/spools',
    action: 'Finish Tour',
  },
]

const TOUR_STORAGE_KEY = 'odin_onboarding_complete'

export default function OnboardingTour() {
  const [isVisible, setIsVisible] = useState(false)
  const [currentStep, setCurrentStep] = useState(0)
  const [isMinimized, setIsMinimized] = useState(false)
  const navigate = useNavigate()
  const location = useLocation()

  useEffect(() => {
    // Show tour if not completed and on dashboard
    const completed = localStorage.getItem(TOUR_STORAGE_KEY)
    if (!completed && location.pathname === '/') {
      // Small delay so dashboard renders first
      const timer = setTimeout(() => setIsVisible(true), 1500)
      return () => clearTimeout(timer)
    }
  }, [location.pathname])

  const completeTour = useCallback(() => {
    localStorage.setItem(TOUR_STORAGE_KEY, 'true')
    setIsVisible(false)
  }, [])

  const nextStep = useCallback(() => {
    if (currentStep < TOUR_STEPS.length - 1) {
      const next = currentStep + 1
      setCurrentStep(next)
      navigate(TOUR_STEPS[next].targetRoute)
    } else {
      completeTour()
    }
  }, [currentStep, navigate, completeTour])

  const prevStep = useCallback(() => {
    if (currentStep > 0) {
      const prev = currentStep - 1
      setCurrentStep(prev)
      navigate(TOUR_STEPS[prev].targetRoute)
    }
  }, [currentStep, navigate])

  if (!isVisible) return null

  const step = TOUR_STEPS[currentStep]
  const isLast = currentStep === TOUR_STEPS.length - 1
  const progress = ((currentStep + 1) / TOUR_STEPS.length) * 100

  if (isMinimized) {
    return (
      <button
        onClick={() => setIsMinimized(false)}
        className="fixed bottom-6 right-6 z-50 flex items-center gap-2 px-4 py-2 rounded-full bg-accent text-accent-foreground shadow-lg hover:brightness-110 transition-all"
        title="Resume tour"
      >
        <Sparkles size={16} />
        <span className="text-sm font-medium">Resume Tour ({currentStep + 1}/{TOUR_STEPS.length})</span>
      </button>
    )
  }

  return (
    <div className="fixed bottom-6 right-6 z-50 w-[420px] max-w-[calc(100vw-2rem)]">
      {/* Card */}
      <div className="rounded-xl border border-border bg-card shadow-2xl overflow-hidden">
        {/* Progress bar */}
        <div className="h-1 bg-muted">
          <div
            className="h-full bg-accent transition-all duration-500"
            style={{ width: `${progress}%` }}
          />
        </div>

        {/* Header */}
        <div className="flex items-center justify-between px-4 pt-3 pb-1">
          <div className="flex items-center gap-2 text-accent">
            <Sparkles size={14} />
            <span className="text-xs font-medium uppercase tracking-wider">
              Getting Started — {currentStep + 1} of {TOUR_STEPS.length}
            </span>
          </div>
          <div className="flex items-center gap-1">
            <button
              onClick={() => setIsMinimized(true)}
              className="p-1 rounded hover:bg-muted transition-colors"
              title="Minimize"
            >
              <ArrowRight size={14} className="text-muted-foreground rotate-90" />
            </button>
            <button
              onClick={completeTour}
              className="p-1 rounded hover:bg-muted transition-colors"
              title="Skip tour"
            >
              <X size={14} className="text-muted-foreground" />
            </button>
          </div>
        </div>

        {/* Content */}
        <div className="px-4 pb-4">
          <div className="flex items-start gap-3 mb-3">
            <div className="mt-0.5 p-2 rounded-lg bg-accent/10 text-accent shrink-0">
              {step.icon}
            </div>
            <div>
              <h3 className="font-semibold text-foreground text-base">{step.title}</h3>
              <p className="text-sm text-muted-foreground mt-1 leading-relaxed">
                {step.description}
              </p>
            </div>
          </div>

          {/* Actions */}
          <div className="flex items-center justify-between mt-4">
            <button
              onClick={prevStep}
              disabled={currentStep === 0}
              className="flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
            >
              <ArrowLeft size={14} />
              Back
            </button>

            <Button
              onClick={nextStep}
              variant="primary"
              size="sm"
            >
              {isLast ? (
                <>
                  <Check size={14} />
                  Finish Tour
                </>
              ) : (
                <>
                  {step.action || 'Next'}
                  <ArrowRight size={14} />
                </>
              )}
            </Button>
          </div>
        </div>
      </div>
    </div>
  )
}

/**
 * Reset the tour — call from Settings > Help.
 */
export function resetOnboardingTour() {
  localStorage.removeItem(TOUR_STORAGE_KEY)
  window.location.href = '/'
}
