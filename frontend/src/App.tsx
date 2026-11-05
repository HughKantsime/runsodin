import { FleetSummary } from './components/foundry/Floor'
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
import Toast from './components/ui/Toast'
import FoundryPage from './components/foundry/Page'
import ProGate from './components/shared/ProGate'
import OnboardingTour from './components/shared/OnboardingTour'
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
import EducationWorkbench from './pages/education/EducationWorkbench'
import { setup } from './api'
import useWebSocket from './hooks/useWebSocket'
import useKeyboardShortcuts from './hooks/useKeyboardShortcuts'
import KeyboardShortcutsModal from './components/shared/KeyboardShortcutsModal'
import Sidebar from './components/layout/Sidebar'
import MobileHeader from './components/layout/MobileHeader'
import MobileBottomNav from './components/ui/MobileBottomNav'
import ThemeToggle from './components/layout/ThemeToggle'
import ProtectedRoute from './components/auth/ProtectedRoute'
import CameraPipProvider from './components/printers/CameraPipProvider'
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
    return <FoundryPage name="/setup"><Setup /></FoundryPage>
  }

  // Show login page without sidebar
  if (location.pathname === '/login') {
    return <FoundryPage name="/login"><Login /></FoundryPage>
  }

  // Password reset — no auth, no sidebar
  if (location.pathname === '/reset-password') {
    return <FoundryPage name="/reset-password"><ResetPassword /></FoundryPage>
  }

  // OBS streaming overlay — no auth, no sidebar
  if (location.pathname.startsWith('/overlay/')) {
    return <Routes><Route path="/overlay/:printerId" element={<FoundryPage name="/overlay/:printerId"><Overlay /></FoundryPage>} /></Routes>
  }

  // TV dashboard mode — full viewport, no sidebar
  if (location.pathname === '/tv') {
    return <FoundryPage name="/tv"><ProtectedRoute><TVDashboard /></ProtectedRoute></FoundryPage>
  }

  return (
    <ProtectedRoute>
      <CameraPipProvider>
      <div className="h-screen flex flex-col md:flex-row overflow-hidden">
        {/* Skip to content — WCAG 2.4.1 */}
        <a href="#main-content" className="sr-only focus:not-sr-only focus:absolute focus:z-[100] focus:top-2 focus:left-2 focus:px-4 focus:py-2 focus:bg-[var(--brand-primary)] focus:text-[var(--brand-on-primary)] focus:rounded-lg focus:text-sm">
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
        <div className="flex-1 flex flex-col overflow-hidden mb-[calc(3.5rem+env(safe-area-inset-bottom))] md:mb-0">
          {/* Desktop search header */}
          <div className="hidden md:flex items-center justify-end gap-3 px-4 py-2 border-b border-[var(--brand-card-border)]" role="toolbar" aria-label="Global actions" style={{ backgroundColor: 'var(--brand-content-bg)' }}>
            <FleetSummary />
            <GlobalSearch />
            <ThemeToggle />
            <AlertBell />
          </div>
          <UpgradeBanner />
          <main id="main-content" tabIndex={-1} className="flex-1 overflow-auto" style={{ backgroundColor: 'var(--brand-content-bg)' }}>
          {/* Screen reader live region for async status announcements */}
          <div role="status" aria-live="polite" aria-atomic="true" className="sr-only" id="sr-status" />
          <ErrorBoundary>
          <Routes>
            <Route path="/" element={<FoundryPage name="/"><RoleGate page="dashboard"><Dashboard /></RoleGate></FoundryPage>} />
            <Route path="/timeline" element={<FoundryPage name="/timeline"><RoleGate page="timeline"><Timeline /></RoleGate></FoundryPage>} />
            <Route path="/jobs" element={<FoundryPage name="/jobs"><RoleGate page="jobs"><Jobs /></RoleGate></FoundryPage>} />
            <Route path="/printers" element={<FoundryPage name="/printers"><RoleGate page="printers"><Printers /></RoleGate></FoundryPage>} />
            <Route path="/printers/:id" element={<FoundryPage name="/printers/:id"><RoleGate page="printers"><PrinterDetail /></RoleGate></FoundryPage>} />
            <Route path="/models" element={<FoundryPage name="/models"><RoleGate page="models"><Models /></RoleGate></FoundryPage>} />
            <Route path="/profiles" element={<FoundryPage name="/profiles"><Profiles /></FoundryPage>} />
            <Route path="/calculator" element={<FoundryPage name="/calculator"><RoleGate page="calculator"><CalculatorPage /></RoleGate></FoundryPage>} />
            <Route path="/analytics" element={<FoundryPage name="/analytics"><RoleGate page="analytics"><ProGate feature="analytics"><Analytics /></ProGate></RoleGate></FoundryPage>} />
            <Route path="/utilization" element={<FoundryPage name="/utilization"><RoleGate page="analytics"><ProGate feature="analytics"><Utilization /></ProGate></RoleGate></FoundryPage>} />
            <Route path="/upload" element={<FoundryPage name="/upload"><RoleGate page="upload"><Upload /></RoleGate></FoundryPage>} />
            <Route path="/spools" element={<FoundryPage name="/spools"><RoleGate page="spools"><Spools /></RoleGate></FoundryPage>} />
            <Route path="/consumables" element={<FoundryPage name="/consumables"><RoleGate page="products"><ProGate feature="products"><Consumables /></ProGate></RoleGate></FoundryPage>} />
            <Route path="/settings" element={<FoundryPage name="/settings"><RoleGate page="settings"><SettingsPage /></RoleGate></FoundryPage>} />
            <Route path="/admin" element={<FoundryPage name="/admin"><RoleGate page="admin"><Navigate to="/settings" replace /></RoleGate></FoundryPage>} />
            <Route path="/permissions" element={<FoundryPage name="/permissions"><RoleGate page="admin"><ProGate feature="permissions"><Navigate to="/settings" replace /></ProGate></RoleGate></FoundryPage>} />
            <Route path="/maintenance" element={<FoundryPage name="/maintenance"><RoleGate page="maintenance"><ProGate feature="maintenance"><Maintenance /></ProGate></RoleGate></FoundryPage>} />
            <Route path="/cameras" element={<FoundryPage name="/cameras"><RoleGate page="cameras"><Cameras /></RoleGate></FoundryPage>} />
            <Route path="/cameras/:id" element={<FoundryPage name="/cameras/:id"><RoleGate page="cameras"><CameraDetail /></RoleGate></FoundryPage>} />
            <Route path="/branding" element={<FoundryPage name="/branding"><RoleGate page="branding"><ProGate feature="branding"><Navigate to="/settings" replace /></ProGate></RoleGate></FoundryPage>} />
            <Route path="/products" element={<FoundryPage name="/products"><RoleGate page="products"><ProGate feature="products"><Products /></ProGate></RoleGate></FoundryPage>} />
            <Route path="/orders" element={<FoundryPage name="/orders"><RoleGate page="orders"><ProGate feature="orders"><Orders /></ProGate></RoleGate></FoundryPage>} />
            <Route path="/alerts" element={<FoundryPage name="/alerts"><RoleGate page="alerts"><Alerts /></RoleGate></FoundryPage>} />
            <Route path="/detections" element={<FoundryPage name="/detections"><Detections /></FoundryPage>} />
            <Route path="/education-reports" element={<FoundryPage name="/education-reports"><RoleGate page="education_reports"><ProGate feature="usage_reports" tier="Pro"><EducationReports /></ProGate></RoleGate></FoundryPage>} />
            <Route path="/education" element={<FoundryPage name="/education"><RoleGate page="education"><ProGate feature="education_workflows" tier="Education"><EducationWorkbench /></ProGate></RoleGate></FoundryPage>} />
            <Route path="/timelapses" element={<FoundryPage name="/timelapses"><Timelapses /></FoundryPage>} />
            <Route path="/archives" element={<FoundryPage name="/archives"><ArchivesPage /></FoundryPage>} />
            <Route path="/projects" element={<FoundryPage name="/projects"><ProjectsPage /></FoundryPage>} />
            <Route path="/print-log" element={<FoundryPage name="/print-log"><PrintLog /></FoundryPage>} />
            <Route path="/audit" element={<FoundryPage name="/audit"><RoleGate page="audit"><AuditLogs /></RoleGate></FoundryPage>} />
            <Route path="*" element={<FoundryPage name="*"><NotFound /></FoundryPage>} />
          </Routes>
          </ErrorBoundary>
          <Toast />
          <OnboardingTour />
      {showHelp && <KeyboardShortcutsModal onClose={() => setShowHelp(false)} />}
            <EmergencyStop />
            <div className="text-center py-4 text-[10px] text-[var(--brand-text-muted)] select-none">Powered by O.D.I.N.</div>
            {/* Spacer for mobile bottom nav */}
            <div className="h-14 md:hidden" />
          </main>
        </div>
        <MobileBottomNav />
      </div>
      </CameraPipProvider>
    </ProtectedRoute>
  )
}
