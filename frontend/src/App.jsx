import { Routes, Route, NavLink } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { 
  LayoutDashboard, 
  Calendar, 
  Printer, 
  Package, 
  ListTodo,
  Settings,
  Activity,
  BarChart3,
  Calculator,
  Upload as UploadIcon,
  Shield,
  LogOut
} from 'lucide-react'
import clsx from 'clsx'

import Dashboard from './pages/Dashboard'
import Timeline from './pages/Timeline'
import Jobs from './pages/Jobs'
import Printers from './pages/Printers'
import Models from './pages/Models'
import CalculatorPage from './pages/Calculator'
import Analytics from './pages/Analytics'
import SettingsPage from './pages/Settings'
import Spools from './pages/Spools'
import Upload from './pages/Upload'
import Login from './pages/Login'
import Admin from './pages/Admin'
import { stats } from './api'

function NavItem({ to, icon: Icon, children }) {
  return (
    <NavLink
      to={to}
      className={({ isActive }) =>
        clsx(
          'flex items-center gap-3 px-4 py-2.5 rounded-lg transition-colors',
          isActive 
            ? 'bg-farm-800 text-print-400' 
            : 'text-farm-400 hover:bg-farm-900 hover:text-farm-200'
        )
      }
    >
      <Icon size={20} />
      <span className="font-medium">{children}</span>
    </NavLink>
  )
}

function Sidebar() {
  const { data: statsData } = useQuery({
    queryKey: ['stats'],
    queryFn: stats.get,
    refetchInterval: 30000,
  })

  return (
    <aside className="w-64 bg-farm-950 border-r border-farm-800 flex flex-col">
      {/* Logo */}
      <div className="p-6 border-b border-farm-800">
        <h1 className="text-xl font-display font-bold text-farm-100">
          PrintFarm
        </h1>
        <p className="text-sm text-farm-500 mt-1">Scheduler</p>
      </div>

      {/* Navigation */}
      <nav className="flex-1 p-4 space-y-1">
        <NavItem to="/" icon={LayoutDashboard}>Dashboard</NavItem>
        <NavItem to="/timeline" icon={Calendar}>Timeline</NavItem>
        <NavItem to="/jobs" icon={ListTodo}>Jobs</NavItem>
        <NavItem to="/upload" icon={UploadIcon}>Upload</NavItem>
        <NavItem to="/printers" icon={Printer}>Printers</NavItem>
        <NavItem to="/models" icon={Package}>Models</NavItem>
        <NavItem to="/calculator" icon={Calculator}>Calculator</NavItem>
        <NavItem to="/analytics" icon={BarChart3}>Analytics</NavItem>
        <NavItem to="/spools" icon={Package}>Spools</NavItem>
        <NavItem to="/settings" icon={Settings}>Settings</NavItem>
        <NavItem to="/admin" icon={Shield}>Admin</NavItem>
      </nav>

      {/* Quick Stats */}
      {statsData && (
        <div className="p-4 border-t border-farm-800">
          <div className="grid grid-cols-2 gap-3">
            <div className="bg-farm-900 rounded-lg p-3">
              <div className="text-2xl font-bold text-print-400">
                {statsData.jobs?.printing || 0}
              </div>
              <div className="text-xs text-farm-500">Printing</div>
            </div>
            <div className="bg-farm-900 rounded-lg p-3">
              <div className="text-2xl font-bold text-status-pending">
                {statsData.jobs?.pending || 0}
              </div>
              <div className="text-xs text-farm-500">Pending</div>
            </div>
          </div>
        </div>
      )}

      {/* Footer */}
      <div className="p-4 border-t border-farm-800">
        <div className="flex items-center gap-2 text-farm-500 text-sm">
          <Activity size={14} className="text-print-500" />
          <span>System Online</span>
        </div>
        <button onClick={() => { localStorage.removeItem("token"); localStorage.removeItem("user"); window.location.href = "/login"; }} className="flex items-center gap-2 text-farm-500 hover:text-red-400 text-sm mt-2 transition-colors"><LogOut size={14} />Logout</button>
      </div>
    </aside>
  )
}

export default function App() {
  return (
    <div className="h-screen flex">
      <Sidebar />
      
      <main className="flex-1 overflow-auto bg-farm-950">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/timeline" element={<Timeline />} />
          <Route path="/jobs" element={<Jobs />} />
          <Route path="/printers" element={<Printers />} />
          <Route path="/models" element={<Models />} />
            <Route path="/calculator" element={<CalculatorPage />} />
            <Route path="/analytics" element={<Analytics />} />
          <Route path="/upload" element={<Upload />} />
          <Route path="/spools" element={<Spools />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/admin" element={<Admin />} />
          <Route path="/login" element={<Login />} />
        </Routes>
      </main>
    </div>
  )
}
