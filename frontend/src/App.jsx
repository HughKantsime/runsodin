// Self-hosted fonts (GDPR — no Google Fonts CDN)
// Default theme fonts — always loaded
import '@fontsource/ibm-plex-mono/400.css'
import '@fontsource/ibm-plex-mono/400-italic.css'
import '@fontsource/ibm-plex-mono/500.css'
import '@fontsource/ibm-plex-mono/600.css'
import '@fontsource/ibm-plex-mono/700.css'
import '@fontsource/ibm-plex-sans/300.css'
import '@fontsource/ibm-plex-sans/400.css'
import '@fontsource/ibm-plex-sans/500.css'
import '@fontsource/ibm-plex-sans/600.css'
import '@fontsource/ibm-plex-sans/700.css'
// Branding-selectable fonts (Inter, JetBrains Mono, Space Grotesk) are
// lazy-loaded by BrandingContext when selected — see loadBrandingFonts()

import { useState, useEffect } from "react"
import { Routes, Route, Navigate, useLocation } from 'react-router-dom'
import { Toaster } from 'react-hot-toast'
import ProGate from './components/shared/ProGate'
import AlertBell from './components/notifications/AlertBell'
import EmergencyStop from './components/printers/EmergencyStop'
import UpgradeBanner from './components/shared/UpgradeBanner'
import GlobalSearch from './components/shared/GlobalSearch'
import ErrorBoundary from './components/shared/ErrorBoundary'
import Dashboard from './pages/dashboard/Dashboard'
import Timeline from './pages/jobs/Timeline'
import Jobs from './pages/jobs/Jobs'
import Printers from './pages/printers/Printers'
import PrinterDetail from './pages/printers/PrinterDetail'
import Models from './pages/models/Models'
import CalculatorPage from './pages/orders/Calculator'
import Analytics from './pages/analytics/Analytics'
import Utilization from './pages/analytics/Utilization'
import SettingsPage from './pages/admin/Settings'
import Spools from './pages/inventory/Spools'
import Consumables from './pages/inventory/Consumables'
import Upload from './pages/jobs/Upload'
import Login from './pages/auth/Login'
import Setup from './pages/auth/Setup'
import Maintenance from './pages/printers/Maintenance'
import Cameras from "./pages/printers/Cameras"
import CameraDetail from "./pages/printers/CameraDetail"
import TVDashboard from "./pages/dashboard/TVDashboard"
import Products from './pages/orders/Products'
import Orders from './pages/orders/Orders'
import Alerts from './pages/notifications/Alerts'
import Detections from './pages/vision/Detections'
import EducationReports from './pages/analytics/EducationReports'
import AuditLogs from './pages/admin/AuditLogs'
import Timelapses from './pages/archives/Timelapses'
import Overlay from './pages/dashboard/Overlay'
import ResetPassword from './pages/auth/ResetPassword'
import ArchivesPage from './pages/archives/Archives'
import PrintLog from './pages/analytics/PrintLog'
import ProjectsPage from './pages/models/Projects'
import Profiles from './pages/models/Profiles'
import { setup } from './api'
import useWebSocket from './hooks/useWebSocket'
import useKeyboardShortcuts from './hooks/useKeyboardShortcuts'
import KeyboardShortcutsModal from './components/shared/KeyboardShortcutsModal'
import Sidebar from './components/layout/Sidebar'
import MobileHeader from './components/layout/MobileHeader'
import ThemeToggle from './components/layout/ThemeToggle'
import ProtectedRoute from './components/auth/ProtectedRoute'
import RoleGate from './components/auth/RoleGate'
import NotFound from './components/layout/NotFound'


// ThemeToggle, MobileHeader, ProtectedRoute, RoleGate, NotFound
// extracted to components/layout/ and components/auth/


export default function App() {

  useWebSocket()
  const { showHelp, setShowHelp } = useKeyboardShortcuts()

  const location = useLocation()
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false)

  // Close mobile menu on route change
  useEffect(() => {
    setMobileMenuOpen(false)
  }, [location.pathname])

  // Check if first-time setup is needed
  useEffect(() => {
    if (location.pathname === '/setup' || location.pathname === '/tv') return
    setup.status()
      .then(data => {
        if (data.needs_setup) {
          window.location.href = '/setup'
        }
      })
      .catch(() => {})
  }, [])

  // Show setup wizard without sidebar
  if (location.pathname === '/setup') {
    return <Setup />
  }

  // Show login page without sidebar
  if (location.pathname === '/login') {
    return <Login />
  }

  // Password reset — no auth, no sidebar
  if (location.pathname === '/reset-password') {
    return <ResetPassword />
  }

  // OBS streaming overlay — no auth, no sidebar
  if (location.pathname.startsWith('/overlay/')) {
    return <Routes><Route path="/overlay/:printerId" element={<Overlay />} /></Routes>
  }

  // TV dashboard mode — full viewport, no sidebar
  if (location.pathname === '/tv') {
    return <ProtectedRoute><TVDashboard /></ProtectedRoute>
  }

  return (
    <ProtectedRoute>
      <div className="h-screen flex flex-col md:flex-row overflow-hidden">
        {/* Skip to content — WCAG 2.4.1 */}
        <a href="#main-content" className="sr-only focus:not-sr-only focus:absolute focus:z-[100] focus:top-2 focus:left-2 focus:px-4 focus:py-2 focus:bg-print-600 focus:text-white focus:rounded-lg focus:text-sm">
          Skip to content
        </a>

        {/* Mobile header with hamburger */}
        <MobileHeader onMenuClick={() => setMobileMenuOpen(true)} />

        {/* Sidebar - hidden on mobile until opened */}
        <Sidebar
          mobileOpen={mobileMenuOpen}
          onMobileClose={() => setMobileMenuOpen(false)}
        />

        {/* Main content */}
        <div className="flex-1 flex flex-col overflow-hidden">
          {/* Desktop search header */}
          <div className="hidden md:flex items-center justify-end gap-3 px-4 py-2 border-b border-farm-800" role="toolbar" aria-label="Global actions" style={{ backgroundColor: 'var(--brand-content-bg)' }}>
            <GlobalSearch />
            <ThemeToggle />
            <AlertBell />
          </div>
          <UpgradeBanner />
          <main id="main-content" className="flex-1 overflow-auto" style={{ backgroundColor: 'var(--brand-content-bg)' }}>
          {/* Screen reader live region for async status announcements */}
          <div role="status" aria-live="polite" aria-atomic="true" className="sr-only" id="sr-status" />
          <ErrorBoundary>
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/timeline" element={<Timeline />} />
            <Route path="/jobs" element={<Jobs />} />
            <Route path="/printers" element={<Printers />} />
            <Route path="/printers/:id" element={<PrinterDetail />} />
            <Route path="/models" element={<Models />} />
            <Route path="/profiles" element={<Profiles />} />
            <Route path="/calculator" element={<CalculatorPage />} />
            <Route path="/analytics" element={<ProGate feature="analytics"><Analytics /></ProGate>} />
            <Route path="/utilization" element={<ProGate feature="analytics"><Utilization /></ProGate>} />
            <Route path="/upload" element={<Upload />} />
            <Route path="/spools" element={<Spools />} />
            <Route path="/consumables" element={<ProGate feature="products"><Consumables /></ProGate>} />
            <Route path="/settings" element={<RoleGate page="settings"><SettingsPage /></RoleGate>} />
            <Route path="/admin" element={<Navigate to="/settings" replace />} />
            <Route path="/permissions" element={<ProGate feature="permissions"><Navigate to="/settings" replace /></ProGate>} />
            <Route path="/maintenance" element={<ProGate feature="maintenance"><Maintenance /></ProGate>} />
            <Route path="/cameras" element={<Cameras />} />
            <Route path="/cameras/:id" element={<CameraDetail />} />
            <Route path="/branding" element={<ProGate feature="branding"><Navigate to="/settings" replace /></ProGate>} />
            <Route path="/products" element={<ProGate feature="products"><Products /></ProGate>} />
            <Route path="/orders" element={<ProGate feature="orders"><Orders /></ProGate>} />
            <Route path="/alerts" element={<Alerts />} />
            <Route path="/detections" element={<Detections />} />
            <Route path="/education-reports" element={<ProGate feature="usage_reports" tier="Pro"><EducationReports /></ProGate>} />
            <Route path="/timelapses" element={<Timelapses />} />
            <Route path="/archives" element={<ArchivesPage />} />
            <Route path="/projects" element={<ProjectsPage />} />
            <Route path="/print-log" element={<PrintLog />} />
            <Route path="/audit" element={<RoleGate page="audit"><AuditLogs /></RoleGate>} />
            <Route path="*" element={<NotFound />} />
          </Routes>
          </ErrorBoundary>
          <Toaster position="top-right" toastOptions={{ style: { background: 'var(--brand-card-bg)', color: 'var(--brand-text-primary)', border: '1px solid var(--brand-card-border)' } }} />
      {showHelp && <KeyboardShortcutsModal onClose={() => setShowHelp(false)} />}
            <EmergencyStop />
            <div className="text-center py-4 text-[10px] text-farm-600 select-none">Powered by O.D.I.N.</div>
          </main>
        </div>
      </div>
    </ProtectedRoute>
  )
}
