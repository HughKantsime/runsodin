import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { User, Lock, Mail, Server, Wifi, CheckCircle2, ChevronRight, ChevronLeft, Loader2, AlertCircle, Printer, Tag, Hash, KeyRound, MonitorSmartphone, ArrowRight, Sparkles } from 'lucide-react'
import { refreshPermissions } from '../permissions'

const STEPS = [
  { id: 'welcome', title: 'Welcome' },
  { id: 'admin', title: 'Create Admin' },
  { id: 'printer', title: 'Add Printer' },
  { id: 'network', title: 'Network' },
  { id: 'done', title: 'Ready' },
]

const PRINTER_MODELS = {
  bambu: [
    { value: 'X1C', label: 'X1 Carbon', slots: 4 },
    { value: 'X1E', label: 'X1E', slots: 4 },
    { value: 'P1S', label: 'P1S', slots: 4 },
    { value: 'P1P', label: 'P1P', slots: 1 },
    { value: 'A1', label: 'A1', slots: 4 },
    { value: 'A1 Mini', label: 'A1 Mini', slots: 4 },
    { value: 'H2D', label: 'H2D', slots: 4 },
  ],
  moonraker: [
    { value: 'Kobra S1', label: 'Anycubic Kobra S1', slots: 1 },
    { value: 'Ender 3', label: 'Creality Ender 3', slots: 1 },
    { value: 'Voron', label: 'Voron', slots: 1 },
    { value: 'Other', label: 'Other Klipper Printer', slots: 1 },
  ],
  prusalink: [
    { value: 'MK4S', label: 'Prusa MK4S', slots: 1 },
    { value: 'MK4', label: 'Prusa MK4', slots: 1 },
    { value: 'MK3.9', label: 'Prusa MK3.9', slots: 1 },
    { value: 'MK3.5', label: 'Prusa MK3.5', slots: 1 },
    { value: 'MINI+', label: 'Prusa MINI+', slots: 1 },
    { value: 'XL', label: 'Prusa XL', slots: 5 },
    { value: 'CORE One', label: 'Prusa CORE One', slots: 1 },
  ],
  elegoo: [
    { value: 'Centauri Carbon', label: 'Centauri Carbon', slots: 1 },
    { value: 'Neptune 4 Pro', label: 'Neptune 4 Pro', slots: 1 },
    { value: 'Neptune 4 Plus', label: 'Neptune 4 Plus', slots: 1 },
    { value: 'Neptune 4 Max', label: 'Neptune 4 Max', slots: 1 },
    { value: 'Saturn 4 Ultra', label: 'Saturn 4 Ultra (Resin)', slots: 1 },
    { value: 'Other', label: 'Other Elegoo Printer', slots: 1 },
  ],
}

export default function Setup() {
  const navigate = useNavigate()
  const [step, setStep] = useState(0)
  const [error, setError] = useState('')
  const [isLoading, setIsLoading] = useState(false)
  const [token, setToken] = useState(null)

  // Admin form
  const [username, setUsername] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [siteName, setSiteName] = useState('O.D.I.N.')

  // Printer form
  const [printerType, setPrinterType] = useState('bambu')
  const [printerModel, setPrinterModel] = useState('')
  const [printerName, setPrinterName] = useState('')
  const [printerIp, setPrinterIp] = useState('')
  const [serial, setSerial] = useState('')
  const [accessCode, setAccessCode] = useState('')
  const [slotCount, setSlotCount] = useState(4)
  const [testResult, setTestResult] = useState(null)
  const [testLoading, setTestLoading] = useState(false)
  const [printerAdded, setPrinterAdded] = useState(false)
  const [addedPrinters, setAddedPrinters] = useState([])
  // Network form
  const [hostIp, setHostIp] = useState('')
  const [detectedIp, setDetectedIp] = useState('')
  const [networkSaved, setNetworkSaved] = useState(false)

  // Auto-set slot count when model changes
  useEffect(() => {
    const models = PRINTER_MODELS[printerType] || []
    const found = models.find(m => m.value === printerModel)
    if (found) {
      setSlotCount(found.slots)
      if (!printerName) {
        setPrinterName(found.label)
      }
    }
  }, [printerModel, printerType])

  // Reset printer fields when type changes
  useEffect(() => {
    setPrinterModel('')
    setPrinterName('')
    setSerial('')
    setAccessCode('')
    setTestResult(null)
    setPrinterAdded(false)
    setSlotCount(['moonraker', 'prusalink', 'elegoo'].includes(printerType) ? 1 : 4)
  }, [printerType])

  const apiHeaders = (extraToken) => ({
    'Content-Type': 'application/json',
    ...(extraToken ? { Authorization: `Bearer ${extraToken}` } : {}),
    ...(import.meta.env.VITE_API_KEY ? { 'X-API-Key': import.meta.env.VITE_API_KEY } : {}),
  })

  // === Step handlers ===

  const handleCreateAdmin = async () => {
    setError('')
    if (!username.trim()) return setError('Username is required')
    if (!email.trim()) return setError('Email is required')
    if (!password) return setError('Password is required')
    if (password.length < 8) return setError('Password must be at least 8 characters')
    if (!/[A-Z]/.test(password)) return setError('Password must contain at least one uppercase letter')
    if (!/[a-z]/.test(password)) return setError('Password must contain at least one lowercase letter')
    if (!/[0-9]/.test(password)) return setError('Password must contain at least one number')
    if (password !== confirmPassword) return setError('Passwords do not match')

    setIsLoading(true)
    try {
      const resp = await fetch('/api/setup/admin', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: username.trim(), email: email.trim(), password, site_name: siteName.trim() || 'O.D.I.N.' }),
      })
      if (!resp.ok) {
        const data = await resp.json()
        throw new Error(data.detail || 'Failed to create admin')
      }
      const data = await resp.json()
      setToken(data.access_token)
      // Store token + user for later
      localStorage.setItem('token', data.access_token)
      localStorage.setItem('user', JSON.stringify({ username: data.username, role: data.role }))
      setStep(2) // Go to printer step
    } catch (err) {
      setError(err.message)
    } finally {
      setIsLoading(false)
    }
  }

  const handleTestPrinter = async () => {
    setTestResult(null)
    setError('')
    if (!printerIp.trim()) return setError('IP address is required')
    if (printerType === 'bambu' && (!serial.trim() || !accessCode.trim())) {
      return setError('Serial number and access code are required for Bambu printers')
    }

    setTestLoading(true)
    try {
      const authToken = token || localStorage.getItem('token')
      const resp = await fetch('/api/setup/test-printer', {
        method: 'POST',
        headers: apiHeaders(authToken),
        body: JSON.stringify({
          name: printerName.trim() || 'Printer',
          printer_type: printerType,
          ip_address: printerIp.trim(),
          model: printerModel,
          serial: serial.trim() || null,
          access_code: accessCode.trim() || null,
          slot_count: slotCount,
        }),
      })
      const data = await resp.json()
      setTestResult(data)
    } catch (err) {
      setTestResult({ success: false, error: err.message })
    } finally {
      setTestLoading(false)
    }
  }

  const handleAddPrinter = async () => {
    setError('')
    if (!printerName.trim()) return setError('Printer name is required')
    if (!printerIp.trim()) return setError('IP address is required')

    setIsLoading(true)
    try {
      const authToken = token || localStorage.getItem('token')
      const resp = await fetch('/api/setup/printer', {
        method: 'POST',
        headers: apiHeaders(authToken),
        body: JSON.stringify({
          name: printerName.trim(),
          printer_type: printerType,
          ip_address: printerIp.trim(),
          model: printerModel || null,
          serial: serial.trim() || null,
          access_code: accessCode.trim() || null,
          slot_count: slotCount,
        }),
      })
      if (!resp.ok) {
        const data = await resp.json()
        throw new Error(data.detail || 'Failed to add printer')
      }
      const data = await resp.json()
      setPrinterAdded(true)
      setAddedPrinters(prev => [...prev, { name: data.name, id: data.printer_id }])
    } catch (err) {
      setError(err.message)
    } finally {
      setIsLoading(false)
    }
  }

  const handleAddAnother = () => {
    setPrinterName('')
    setPrinterIp('')
    setSerial('')
    setAccessCode('')
    setPrinterModel('')
    setTestResult(null)
    setPrinterAdded(false)
  }

  const handleFinish = async () => {
    try {
      const authToken = token || localStorage.getItem('token')
      await fetch('/api/setup/complete', {
        method: 'POST',
        headers: apiHeaders(authToken),
      })
    } catch {
      // Non-critical
    }
    await refreshPermissions()
    navigate('/')
  }

  // === Render helpers ===

  const stepIndicator = () => (
    <div className="flex items-center justify-center gap-2 mb-8">
      {STEPS.map((s, i) => (
        <div key={s.id} className="flex items-center gap-2">
          <div
            className={`w-8 h-8 rounded-full flex items-center justify-center text-sm font-medium transition-all duration-300 ${
              i < step ? 'bg-emerald-500/20 text-emerald-400 ring-1 ring-emerald-500/40' :
              i === step ? 'bg-amber-500/20 text-amber-400 ring-2 ring-amber-500/60' :
              'bg-white/5 text-white/30 ring-1 ring-white/10'
            }`}
          >
            {i < step ? <CheckCircle2 size={16} /> : i + 1}
          </div>
          {i < STEPS.length - 1 && (
            <div className={`w-8 h-px transition-colors duration-300 ${i < step ? 'bg-emerald-500/40' : 'bg-white/10'}`} />
          )}
        </div>
      ))}
    </div>
  )

  const errorBar = () => error && (
    <div className="mb-4 p-3 bg-red-900/30 border border-red-700/50 rounded-lg flex items-center gap-2 text-sm text-red-300">
      <AlertCircle size={16} className="shrink-0" />
      {error}
    </div>
  )

  const inputClass = "w-full px-3 py-2.5 rounded-lg bg-white/5 border border-white/10 text-white placeholder-white/30 focus:outline-none focus:ring-2 focus:ring-amber-500/50 focus:border-amber-500/50 transition-all"
  const labelClass = "block text-sm font-medium text-white/70 mb-1.5"
  const btnPrimary = "flex items-center justify-center gap-2 px-6 py-2.5 rounded-lg bg-amber-500 hover:bg-amber-400 text-black font-semibold transition-all disabled:opacity-50 disabled:cursor-not-allowed"
  const btnSecondary = "flex items-center justify-center gap-2 px-5 py-2.5 rounded-lg bg-white/5 hover:bg-white/10 text-white/80 border border-white/10 transition-all"

  // === STEP VIEWS ===

  const renderWelcome = () => (
    <div className="text-center">
      <div className="mb-6">
        <div className="inline-flex items-center justify-center w-20 h-20 rounded-lg bg-amber-500/10 ring-1 ring-amber-500/30 mb-4">
          <Sparkles size={36} className="text-amber-400" />
        </div>
        <h1 className="text-3xl font-bold text-white mb-2">Welcome to O.D.I.N.</h1>
        <p className="text-white/50 text-lg">Orchestrated Dispatch & Inventory Network</p>
      </div>
      <p className="text-white/60 mb-8 max-w-md mx-auto leading-relaxed">
        Let's get your print farm set up. This wizard will walk you through creating your admin account and connecting your first printer. It takes about 2 minutes.
      </p>
      <button onClick={() => setStep(1)} className={btnPrimary + " mx-auto"}>
        Get Started <ArrowRight size={18} />
      </button>
    </div>
  )

  const renderAdmin = () => (
    <div>
      <h2 className="text-xl font-bold text-white mb-1">Create Admin Account</h2>
      <p className="text-white/50 text-sm mb-6">This will be your primary login.</p>
      {errorBar()}

      <div className="space-y-4">
        <div>
          <label className={labelClass}>Instance Name</label>
          <div className="relative">
            <Tag size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-white/30" />
            <input type="text" value={siteName} onChange={e => setSiteName(e.target.value)} placeholder="O.D.I.N." className={inputClass + " pl-10"} />
          </div>
          <p className="text-white/30 text-xs mt-1">Shown in header and login page</p>
        </div>

        <div className="border-t border-white/5 pt-4">
          <label className={labelClass}>Username</label>
          <div className="relative">
            <User size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-white/30" />
            <input type="text" value={username} onChange={e => setUsername(e.target.value)} placeholder="admin" className={inputClass + " pl-10"} autoFocus />
          </div>
        </div>

        <div>
          <label className={labelClass}>Email</label>
          <div className="relative">
            <Mail size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-white/30" />
            <input type="email" value={email} onChange={e => setEmail(e.target.value)} placeholder="you@example.com" className={inputClass + " pl-10"} />
          </div>
        </div>

        <div>
          <label className={labelClass}>Password</label>
          <div className="relative">
            <Lock size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-white/30" />
            <input type="password" value={password} onChange={e => setPassword(e.target.value)} placeholder="Min 8 characters (uppercase, lowercase, number)" className={inputClass + " pl-10"} />
          </div>
          {password && (
            <div className="mt-2 space-y-0.5 text-xs">
              <p className={password.length >= 8 ? 'text-emerald-400' : 'text-white/30'}>{password.length >= 8 ? '\u2713' : '\u2717'} At least 8 characters</p>
              <p className={/[A-Z]/.test(password) ? 'text-emerald-400' : 'text-white/30'}>{/[A-Z]/.test(password) ? '\u2713' : '\u2717'} One uppercase letter</p>
              <p className={/[a-z]/.test(password) ? 'text-emerald-400' : 'text-white/30'}>{/[a-z]/.test(password) ? '\u2713' : '\u2717'} One lowercase letter</p>
              <p className={/[0-9]/.test(password) ? 'text-emerald-400' : 'text-white/30'}>{/[0-9]/.test(password) ? '\u2713' : '\u2717'} One number</p>
            </div>
          )}
        </div>

        <div>
          <label className={labelClass}>Confirm Password</label>
          <div className="relative">
            <Lock size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-white/30" />
            <input type="password" value={confirmPassword} onChange={e => setConfirmPassword(e.target.value)} placeholder="Repeat password" className={inputClass + " pl-10"}
              onKeyDown={e => e.key === 'Enter' && handleCreateAdmin()} />
          </div>
        </div>
      </div>

      <div className="flex justify-between mt-8">
        <button onClick={() => setStep(0)} className={btnSecondary}>
          <ChevronLeft size={16} /> Back
        </button>
        <button onClick={handleCreateAdmin} disabled={isLoading} className={btnPrimary}>
          {isLoading ? <><Loader2 size={16} className="animate-spin" /> Creating...</> : <>Create Account <ChevronRight size={16} /></>}
        </button>
      </div>
    </div>
  )

  const renderPrinter = () => (
    <div>
      <h2 className="text-xl font-bold text-white mb-1">Add a Printer</h2>
      <p className="text-white/50 text-sm mb-6">Connect your first printer. You can add more later.</p>
      {errorBar()}

      {printerAdded ? (
        <div className="text-center py-4">
          <div className="inline-flex items-center justify-center w-14 h-14 rounded-full bg-emerald-500/10 ring-1 ring-emerald-500/40 mb-4">
            <CheckCircle2 size={28} className="text-emerald-400" />
          </div>
          <p className="text-white font-medium mb-1">Printer added!</p>
          {addedPrinters.length > 0 && (
            <div className="text-white/40 text-sm mb-6">
              {addedPrinters.map(p => p.name).join(', ')}
            </div>
          )}
          <div className="flex gap-3 justify-center">
            <button onClick={handleAddAnother} className={btnSecondary}>
              <Printer size={16} /> Add Another
            </button>
            <button onClick={() => setStep(3)} className={btnPrimary}>
              Finish Setup <ChevronRight size={16} />
            </button>
          </div>
        </div>
      ) : (
        <>
          <div className="space-y-4">
            {/* Printer type toggle */}
            <div>
              <label className={labelClass}>Printer Type</label>
              <div className="grid grid-cols-2 gap-2">
                <button
                  onClick={() => setPrinterType('bambu')}
                  className={`px-4 py-3 rounded-lg border text-sm font-medium transition-all ${
                    printerType === 'bambu'
                      ? 'bg-amber-500/15 border-amber-500/50 text-amber-300'
                      : 'bg-white/5 border-white/10 text-white/50 hover:bg-white/10'
                  }`}
                >
                  Bambu Lab
                </button>
                <button
                  onClick={() => setPrinterType('moonraker')}
                  className={`px-4 py-3 rounded-lg border text-sm font-medium transition-all ${
                    printerType === 'moonraker'
                      ? 'bg-amber-500/15 border-amber-500/50 text-amber-300'
                      : 'bg-white/5 border-white/10 text-white/50 hover:bg-white/10'
                  }`}
                >
                  Klipper
                </button>
                <button
                  onClick={() => setPrinterType('prusalink')}
                  className={`px-4 py-3 rounded-lg border text-sm font-medium transition-all ${
                    printerType === 'prusalink'
                      ? 'bg-amber-500/15 border-amber-500/50 text-amber-300'
                      : 'bg-white/5 border-white/10 text-white/50 hover:bg-white/10'
                  }`}
                >
                  Prusa
                </button>
                <button
                  onClick={() => setPrinterType('elegoo')}
                  className={`px-4 py-3 rounded-lg border text-sm font-medium transition-all ${
                    printerType === 'elegoo'
                      ? 'bg-amber-500/15 border-amber-500/50 text-amber-300'
                      : 'bg-white/5 border-white/10 text-white/50 hover:bg-white/10'
                  }`}
                >
                  Elegoo
                </button>
              </div>
            </div>

            {/* Model selector */}
            <div>
              <label className={labelClass}>Model</label>
              <select value={printerModel} onChange={e => setPrinterModel(e.target.value)} className={inputClass}>
                <option value="">Select model...</option>
                {(PRINTER_MODELS[printerType] || []).map(m => (
                  <option key={m.value} value={m.value}>{m.label}</option>
                ))}
              </select>
            </div>

            {/* Name */}
            <div>
              <label className={labelClass}>Display Name</label>
              <div className="relative">
                <Tag size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-white/30" />
                <input type="text" value={printerName} onChange={e => setPrinterName(e.target.value)} placeholder="e.g. X1C - Left Shelf" className={inputClass + " pl-10"} />
              </div>
            </div>

            {/* IP */}
            <div>
              <label className={labelClass}>IP Address</label>
              <div className="relative">
                <Wifi size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-white/30" />
                <input type="text" value={printerIp} onChange={e => setPrinterIp(e.target.value)} placeholder="192.168.1.100" className={inputClass + " pl-10"} />
              </div>
            </div>

            {/* Bambu-specific: serial + access code */}
            {printerType === 'bambu' && (
              <>
                <div>
                  <label className={labelClass}>Serial Number</label>
                  <div className="relative">
                    <Hash size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-white/30" />
                    <input type="text" value={serial} onChange={e => setSerial(e.target.value)} placeholder="From printer settings or Bambu app" className={inputClass + " pl-10"} />
                  </div>
                </div>
                <div>
                  <label className={labelClass}>Access Code</label>
                  <div className="relative">
                    <KeyRound size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-white/30" />
                    <input type="text" value={accessCode} onChange={e => setAccessCode(e.target.value)} placeholder="8-digit code from printer" className={inputClass + " pl-10"} />
                  </div>
                </div>
              </>
            )}

            {/* Test result */}
            {testResult && (
              <div className={`p-3 rounded-lg border text-sm ${
                testResult.success
                  ? 'bg-emerald-900/20 border-emerald-700/50 text-emerald-300'
                  : 'bg-red-900/20 border-red-700/50 text-red-300'
              }`}>
                {testResult.success ? (
                  <div className="flex items-center gap-2">
                    <CheckCircle2 size={16} />
                    <span>Connected! {testResult.state && `State: ${testResult.state}`} {testResult.ams_slots !== undefined && `\u2022 ${testResult.ams_slots} AMS slots`}</span>
                  </div>
                ) : (
                  <div className="flex items-center gap-2">
                    <AlertCircle size={16} />
                    <span>{testResult.error || testResult.message || 'Connection failed'}</span>
                  </div>
                )}
              </div>
            )}
          </div>

          <div className="flex items-center justify-between mt-8">
            <button onClick={() => setStep(1)} className={btnSecondary}>
              <ChevronLeft size={16} /> Back
            </button>
            <div className="flex gap-2">
              <button onClick={handleTestPrinter} disabled={testLoading || !printerIp.trim()} className={btnPrimary}>
                {testLoading ? <><Loader2 size={16} className="animate-spin" /> Testing...</> : <>Test Connection</>}
              </button>
              <button onClick={handleAddPrinter} disabled={isLoading} className={btnSecondary}>
                {isLoading ? <><Loader2 size={16} className="animate-spin" /> Adding...</> : <>Add Printer</>}
              </button>
            </div>
          </div>

          <button onClick={() => setStep(3)} className="w-full mt-3 text-center text-white/30 hover:text-white/50 text-sm transition-colors">
            Skip \u2014 I'll add printers later
          </button>
        </>
      )}
    </div>
  )

  const handleSaveNetwork = async () => {
    setError('')
    setIsLoading(true)
    try {
      const authToken = token || localStorage.getItem('token')
      const resp = await fetch('/api/setup/network', {
        method: 'POST',
        headers: apiHeaders(authToken),
        body: JSON.stringify({ host_ip: hostIp })
      })
      if (!resp.ok) {
        const data = await resp.json()
        throw new Error(data.detail || 'Failed to save')
      }
      setNetworkSaved(true)
    } catch (err) {
      setError(err.message)
    } finally {
      setIsLoading(false)
    }
  }

  // Auto-detect network IP when reaching the network step
  useEffect(() => {
    if (step === 3 && !detectedIp) {
      fetch('/api/setup/network')
        .then(r => { if (!r.ok) throw new Error('Failed'); return r.json() })
        .then(data => {
          setDetectedIp(data.detected_ip || '')
          if (!hostIp && data.detected_ip) setHostIp(data.detected_ip)
        })
        .catch(() => {})
    }
  }, [step])

  const renderNetwork = () => {
    return (
      <div>
        <div className="text-center mb-6">
          <h2 className="text-2xl font-bold text-white mb-2">Network Configuration</h2>
          <p className="text-white/50 text-sm max-w-sm mx-auto">
            Set the IP address of this server so cameras can stream to your browser. This is the LAN IP where O.D.I.N. is running.
          </p>
        </div>
        {errorBar()}
        <div className="space-y-4">
          <div>
            <label className={labelClass}>Host IP Address</label>
            <input type="text" value={hostIp} onChange={e => { setHostIp(e.target.value); setNetworkSaved(false) }} placeholder="e.g. 192.168.1.100" className={inputClass} />
            {detectedIp && <p className="text-xs text-white/40 mt-1">Auto-detected: {detectedIp}</p>}
          </div>
          <p className="text-xs text-white/30">This is needed for WebRTC camera streaming. Use the LAN IP that your browser can reach — not 127.0.0.1 or a Docker internal IP.</p>
          {networkSaved && <div className="p-3 bg-emerald-900/30 border border-emerald-700/50 rounded-lg text-sm text-emerald-300 flex items-center gap-2"><CheckCircle2 size={16} /> Network configured successfully</div>}
          <div className="flex gap-3 pt-2">
            <button onClick={handleSaveNetwork} disabled={!hostIp || isLoading} className={btnPrimary + " flex-1"}>
              {isLoading ? 'Saving...' : networkSaved ? 'Saved ✓' : 'Save Network Config'}
            </button>
            <button onClick={() => setStep(step + 1)} className={btnSecondary}>
              {networkSaved ? 'Continue' : 'Skip'}
            </button>
          </div>
        </div>
      </div>
    )
  }

  const renderDone = () => (
    <div className="text-center">
      <div className="mb-6">
        <div className="inline-flex items-center justify-center w-20 h-20 rounded-lg bg-emerald-500/10 ring-1 ring-emerald-500/40 mb-4">
          <CheckCircle2 size={36} className="text-emerald-400" />
        </div>
        <h2 className="text-2xl font-bold text-white mb-2">You're all set!</h2>
        <p className="text-white/50 max-w-sm mx-auto">
          {addedPrinters.length > 0
            ? `${addedPrinters.length} printer${addedPrinters.length > 1 ? 's' : ''} connected. Your dashboard is ready.`
            : 'Your admin account is created. Add printers from the Printers page whenever you\'re ready.'}
        </p>
      </div>

      {addedPrinters.length > 0 && (
        <div className="mb-6 p-4 rounded-lg bg-white/5 border border-white/10 max-w-sm mx-auto">
          <p className="text-white/50 text-xs uppercase tracking-wider mb-2">Connected Printers</p>
          {addedPrinters.map((p, i) => (
            <div key={i} className="flex items-center gap-2 text-white/80 text-sm py-1">
              <Printer size={14} className="text-emerald-400" /> {p.name}
            </div>
          ))}
        </div>
      )}

      <button onClick={handleFinish} className={btnPrimary + " mx-auto text-lg px-8 py-3"}>
        Open Dashboard <ArrowRight size={18} />
      </button>
    </div>
  )

  const stepViews = [renderWelcome, renderAdmin, renderPrinter, renderNetwork, renderDone]

  return (
    <div className="min-h-screen flex items-center justify-center p-4" style={{ backgroundColor: '#0f1117' }}>
      <div className="w-full max-w-lg">
        {stepIndicator()}
        <div className="rounded-lg p-8 bg-[#1a1c25] border border-white/[0.06] shadow-2xl">
          {stepViews[step]()}
        </div>
        <p className="text-center text-white/20 text-xs mt-4">
          O.D.I.N. \u2014 Orchestrated Dispatch & Inventory Network
        </p>
      </div>
    </div>
  )
}
