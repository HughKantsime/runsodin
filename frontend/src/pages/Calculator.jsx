import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { models, pricingConfig } from '../api'
import {
  Calculator,
  Package,
  Clock,
  Zap,
  Printer,
  AlertTriangle,
  Box,
  DollarSign,
  Percent,
  TrendingUp,
  Save,
  RefreshCw
} from 'lucide-react'

function InputField({ label, value, onChange, suffix, icon: Icon, step = "any", min = 0 }) {
  return (
    <div>
      <label className="block text-xs md:text-sm text-farm-400 mb-1">{label}</label>
      <div className="relative">
        {Icon && <Icon size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-farm-500" />}
        <input
          type="number"
          step={step}
          min={min}
          value={value}
          onChange={(e) => onChange(parseFloat(e.target.value) || 0)}
          className={`w-full bg-farm-800 border border-farm-700 rounded-lg py-2 pr-10 text-sm ${Icon ? 'pl-8' : 'pl-3'}`}
        />
        {suffix && <span className="absolute right-3 top-1/2 -translate-y-1/2 text-farm-500 text-xs">{suffix}</span>}
      </div>
    </div>
  )
}

function CostSection({ title, icon: Icon, cost, children }) {
  return (
    <div className="bg-farm-900 rounded border border-farm-800 p-3 md:p-4">
      <div className="flex items-center justify-between mb-3 md:mb-4">
        <div className="flex items-center gap-2">
          {Icon && <Icon size={16} className="text-farm-400" />}
          <h3 className="font-medium text-sm">{title}</h3>
        </div>
        <span className="text-base md:text-lg font-bold text-print-400">${cost.toFixed(2)}</span>
      </div>
      <div className="space-y-3">{children}</div>
    </div>
  )
}

function PricingBreakdown({ costs, margin, quantity }) {
  const subtotal = Object.values(costs).reduce((sum, c) => sum + c, 0)
  const withMargin = subtotal * (1 + margin / 100)
  const perUnit = withMargin
  const total = perUnit * quantity

  return (
    <div className="bg-farm-900 rounded border border-farm-800 p-3 md:p-4 lg:sticky lg:top-4">
      <h3 className="font-display font-semibold mb-4 text-sm md:text-base">Final Pricing</h3>
      <div className="space-y-2 text-sm">
        <div className="flex justify-between"><span className="text-farm-400">Material Cost:</span><span>${costs.material.toFixed(2)}</span></div>
        <div className="flex justify-between"><span className="text-farm-400">Labor Cost:</span><span>${costs.labor.toFixed(2)}</span></div>
        <div className="flex justify-between"><span className="text-farm-400">Electricity:</span><span>${costs.electricity.toFixed(2)}</span></div>
        <div className="flex justify-between"><span className="text-farm-400">Printer Depreciation:</span><span>${costs.depreciation.toFixed(2)}</span></div>
        <div className="flex justify-between"><span className="text-farm-400">Packaging:</span><span>${costs.packaging.toFixed(2)}</span></div>
        <div className="flex justify-between"><span className="text-farm-400">Failure Buffer ({costs.failureRate}%):</span><span>${costs.failure.toFixed(2)}</span></div>
        <div className="flex justify-between"><span className="text-farm-400">Overhead:</span><span>${costs.overhead.toFixed(2)}</span></div>
        <div className="flex justify-between"><span className="text-farm-400">Other Costs:</span><span>${costs.other.toFixed(2)}</span></div>

        <div className="border-t border-farm-700 pt-2 mt-2">
          <div className="flex justify-between font-medium"><span>Subtotal:</span><span>${subtotal.toFixed(2)}</span></div>
        </div>

        <div className="pt-2">
          <label className="block text-sm text-farm-400 mb-1">Manufacturing Margin</label>
          <div className="text-2xl font-bold text-center py-2">{margin}%</div>
        </div>

        <div className="bg-print-900/50 border border-print-700 rounded-lg p-3 md:p-4 text-center">
          <div className="text-xs md:text-sm text-farm-400">Recommended Price (per unit)</div>
          <div className="text-2xl md:text-3xl font-display font-bold text-print-400">${perUnit.toFixed(2)}</div>
        </div>

        {quantity > 1 && (
          <div className="bg-farm-800 rounded-lg p-3 text-center">
            <div className="text-xs md:text-sm text-farm-400">Total for {quantity} units</div>
            <div className="text-lg md:text-xl font-bold text-green-400">${total.toFixed(2)}</div>
          </div>
        )}

        <div className="pt-2 space-y-1 text-xs text-farm-500">
          <div className="flex justify-between"><span>Cost per unit:</span><span>${subtotal.toFixed(2)}</span></div>
          <div className="flex justify-between"><span>Profit per unit:</span><span className="text-green-400">${(withMargin - subtotal).toFixed(2)}</span></div>
          <div className="flex justify-between"><span>Profit margin:</span><span className="text-green-400">{((withMargin - subtotal) / withMargin * 100).toFixed(1)}%</span></div>
        </div>
      </div>
    </div>
  )
}

export default function CalculatorPage() {
  const queryClient = useQueryClient()
  
  // Fetch models for dropdown
  const { data: modelsData } = useQuery({ queryKey: ['models'], queryFn: models.list })
  
  // Fetch pricing config from backend
  const { data: savedConfig, isLoading: configLoading } = useQuery({
    queryKey: ['pricingConfig'],
    queryFn: pricingConfig.get
  })
  
  // Save config mutation
  const saveConfigMutation = useMutation({
    mutationFn: pricingConfig.update,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['pricingConfig'] })
    }
  })

  const [selectedModel, setSelectedModel] = useState('')
  const [quantity, setQuantity] = useState(1)
  const [hasChanges, setHasChanges] = useState(false)
  
  // Pricing config state
  const [filamentGrams, setFilamentGrams] = useState(50)
  const [spoolCost, setSpoolCost] = useState(25)
  const [spoolWeight, setSpoolWeight] = useState(1000)
  const [hourlyRate, setHourlyRate] = useState(15)
  const [postProcessingMin, setPostProcessingMin] = useState(5)
  const [packingMin, setPackingMin] = useState(5)
  const [supportMin, setSupportMin] = useState(5)
  const [electricityRate, setElectricityRate] = useState(0.12)
  const [printerWattage, setPrinterWattage] = useState(100)
  const [printTimeHours, setPrintTimeHours] = useState(2)
  const [printerCost, setPrinterCost] = useState(300)
  const [printerLifespan, setPrinterLifespan] = useState(5000)
  const [packagingCost, setPackagingCost] = useState(0.45)
  const [failureRate, setFailureRate] = useState(7)
  const [monthlyRent, setMonthlyRent] = useState(0)
  const [partsPerMonth, setPartsPerMonth] = useState(100)
  const [otherCosts, setOtherCosts] = useState(0)
  const [margin, setMargin] = useState(50)

  // Load saved config when it arrives
  useEffect(() => {
    if (savedConfig) {
      setSpoolCost(savedConfig.spool_cost ?? 25)
      setSpoolWeight(savedConfig.spool_weight ?? 1000)
      setHourlyRate(savedConfig.hourly_rate ?? 15)
      setPostProcessingMin(savedConfig.post_processing_min ?? 5)
      setPackingMin(savedConfig.packing_min ?? 5)
      setSupportMin(savedConfig.support_min ?? 5)
      setElectricityRate(savedConfig.electricity_rate ?? 0.12)
      setPrinterWattage(savedConfig.printer_wattage ?? 100)
      setPrinterCost(savedConfig.printer_cost ?? 300)
      setPrinterLifespan(savedConfig.printer_lifespan ?? 5000)
      setPackagingCost(savedConfig.packaging_cost ?? 0.45)
      setFailureRate(savedConfig.failure_rate ?? 7)
      setMonthlyRent(savedConfig.monthly_rent ?? 0)
      setPartsPerMonth(savedConfig.parts_per_month ?? 100)
      setOtherCosts(savedConfig.other_costs ?? 0)
      setMargin(savedConfig.default_margin ?? 50)
      setHasChanges(false)
    }
  }, [savedConfig])

  // Load model data when selected
  useEffect(() => {
    if (selectedModel && modelsData) {
      const model = modelsData.find(m => m.id === parseInt(selectedModel))
      if (model) {
        if (model.total_filament_grams) setFilamentGrams(model.total_filament_grams)
        if (model.build_time_hours) setPrintTimeHours(model.build_time_hours)
        if (model.units_per_bed) setQuantity(model.units_per_bed)
      }
    }
  }, [selectedModel, modelsData])

  // Track config changes
  const markChanged = (setter) => (value) => {
    setter(value)
    setHasChanges(true)
  }

  // Save config
  const handleSaveConfig = () => {
    saveConfigMutation.mutate({
      spool_cost: spoolCost,
      spool_weight: spoolWeight,
      hourly_rate: hourlyRate,
      post_processing_min: postProcessingMin,
      packing_min: packingMin,
      support_min: supportMin,
      electricity_rate: electricityRate,
      printer_wattage: printerWattage,
      printer_cost: printerCost,
      printer_lifespan: printerLifespan,
      packaging_cost: packagingCost,
      failure_rate: failureRate,
      monthly_rent: monthlyRent,
      parts_per_month: partsPerMonth,
      other_costs: otherCosts,
      default_margin: margin
    })
    setHasChanges(false)
  }

  // Reset to saved config
  const handleResetConfig = () => {
    if (savedConfig) {
      setSpoolCost(savedConfig.spool_cost ?? 25)
      setSpoolWeight(savedConfig.spool_weight ?? 1000)
      setHourlyRate(savedConfig.hourly_rate ?? 15)
      setPostProcessingMin(savedConfig.post_processing_min ?? 5)
      setPackingMin(savedConfig.packing_min ?? 5)
      setSupportMin(savedConfig.support_min ?? 5)
      setElectricityRate(savedConfig.electricity_rate ?? 0.12)
      setPrinterWattage(savedConfig.printer_wattage ?? 100)
      setPrinterCost(savedConfig.printer_cost ?? 300)
      setPrinterLifespan(savedConfig.printer_lifespan ?? 5000)
      setPackagingCost(savedConfig.packaging_cost ?? 0.45)
      setFailureRate(savedConfig.failure_rate ?? 7)
      setMonthlyRent(savedConfig.monthly_rent ?? 0)
      setPartsPerMonth(savedConfig.parts_per_month ?? 100)
      setOtherCosts(savedConfig.other_costs ?? 0)
      setMargin(savedConfig.default_margin ?? 50)
      setHasChanges(false)
    }
  }

  // Calculations
  const costPerGram = spoolCost / spoolWeight
  const materialCost = filamentGrams * costPerGram
  const laborHours = (postProcessingMin + packingMin + supportMin) / 60
  const laborCost = laborHours * hourlyRate
  const electricityCost = (printerWattage / 1000) * printTimeHours * electricityRate
  const depreciationCost = (printerCost / printerLifespan) * printTimeHours
  const baseCost = materialCost + laborCost + electricityCost + depreciationCost + packagingCost + otherCosts
  const failureCost = baseCost * (failureRate / 100)
  const overheadCost = partsPerMonth > 0 ? monthlyRent / partsPerMonth : 0

  const costs = {
    material: materialCost,
    labor: laborCost,
    electricity: electricityCost,
    depreciation: depreciationCost,
    packaging: packagingCost,
    failureRate: failureRate,
    failure: failureCost,
    overhead: overheadCost,
    other: otherCosts
  }

  if (configLoading) {
    return (
      <div className="p-4 md:p-6 flex items-center justify-center min-h-[400px]">
        <div className="text-farm-400">Loading pricing configuration...</div>
      </div>
    )
  }

  return (
    <div className="p-4 md:p-6">
      <div className="mb-4 md:mb-6">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-xl md:text-2xl font-display font-bold flex items-center gap-2">
              <Calculator className="text-print-400" size={22} />
              Pricing Calculator
            </h1>
            <p className="text-farm-400 text-sm">Calculate the true cost and recommended price for your prints</p>
          </div>
          <div className="flex gap-2">
            {hasChanges && (
              <button
                onClick={handleResetConfig}
                className="flex items-center gap-2 px-3 py-2 bg-farm-800 border border-farm-700 rounded-lg text-sm hover:bg-farm-700"
              >
                <RefreshCw size={16} />
                Reset
              </button>
            )}
            <button
              onClick={handleSaveConfig}
              disabled={!hasChanges || saveConfigMutation.isPending}
              className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
                hasChanges 
                  ? 'bg-print-600 hover:bg-print-500 text-white' 
                  : 'bg-farm-800 text-farm-500 cursor-not-allowed'
              }`}
            >
              <Save size={16} />
              {saveConfigMutation.isPending ? 'Saving...' : 'Save Config'}
            </button>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 md:gap-6">
        <div className="lg:col-span-2 space-y-4">
          {/* Model selector */}
          <div className="bg-farm-900 rounded border border-farm-800 p-3 md:p-4">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 md:gap-4">
              <div>
                <label className="block text-xs md:text-sm text-farm-400 mb-1">Select Model (optional)</label>
                <select
                  value={selectedModel}
                  onChange={(e) => setSelectedModel(e.target.value)}
                  className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm"
                >
                  <option value="">-- Manual Entry --</option>
                  {modelsData?.map(model => (
                    <option key={model.id} value={model.id}>{model.name}</option>
                  ))}
                </select>
              </div>
              <InputField label="Quantity" value={quantity} onChange={setQuantity} suffix="units" min={1} step={1} />
            </div>
          </div>

          {/* Material Costs */}
          <CostSection title="Material Costs" icon={Package} cost={materialCost}>
            <div className="grid grid-cols-3 gap-2 md:gap-3">
              <InputField label="Filament Used" value={filamentGrams} onChange={setFilamentGrams} suffix="g" />
              <InputField label="Spool Cost" value={spoolCost} onChange={markChanged(setSpoolCost)} suffix="$" />
              <InputField label="Spool Weight" value={spoolWeight} onChange={markChanged(setSpoolWeight)} suffix="g" />
            </div>
            <div className="text-xs text-farm-500">Cost per gram: ${costPerGram.toFixed(3)}</div>
          </CostSection>

          {/* Labor Costs */}
          <CostSection title="Labor Costs" icon={Clock} cost={laborCost}>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-2 md:gap-3">
              <InputField label="Hourly Rate" value={hourlyRate} onChange={markChanged(setHourlyRate)} suffix="$/hr" icon={DollarSign} />
              <InputField label="Post-Process" value={postProcessingMin} onChange={markChanged(setPostProcessingMin)} suffix="min" />
              <InputField label="Packing" value={packingMin} onChange={markChanged(setPackingMin)} suffix="min" />
              <InputField label="Support" value={supportMin} onChange={markChanged(setSupportMin)} suffix="min" />
            </div>
          </CostSection>

          {/* Electricity Costs */}
          <CostSection title="Electricity" icon={Zap} cost={electricityCost}>
            <div className="grid grid-cols-3 gap-2 md:gap-3">
              <InputField label="Rate" value={electricityRate} onChange={markChanged(setElectricityRate)} suffix="$/kWh" />
              <InputField label="Printer Watts" value={printerWattage} onChange={markChanged(setPrinterWattage)} suffix="W" />
              <InputField label="Print Time" value={printTimeHours} onChange={setPrintTimeHours} suffix="hrs" />
            </div>
          </CostSection>

          {/* Printer Depreciation */}
          <CostSection title="Printer Depreciation" icon={Printer} cost={depreciationCost}>
            <div className="grid grid-cols-2 gap-2 md:gap-3">
              <InputField label="Printer Cost" value={printerCost} onChange={markChanged(setPrinterCost)} suffix="$" />
              <InputField label="Lifespan" value={printerLifespan} onChange={markChanged(setPrinterLifespan)} suffix="hrs" />
            </div>
          </CostSection>

          {/* Other Costs */}
          <CostSection title="Other Costs" icon={Box} cost={packagingCost + overheadCost + otherCosts}>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-2 md:gap-3">
              <InputField label="Packaging" value={packagingCost} onChange={markChanged(setPackagingCost)} suffix="$" />
              <InputField label="Monthly Rent" value={monthlyRent} onChange={markChanged(setMonthlyRent)} suffix="$" />
              <InputField label="Parts/Month" value={partsPerMonth} onChange={markChanged(setPartsPerMonth)} suffix="pcs" />
              <InputField label="Other" value={otherCosts} onChange={markChanged(setOtherCosts)} suffix="$" />
            </div>
          </CostSection>

          {/* Failure Buffer */}
          <CostSection title="Failure Buffer" icon={AlertTriangle} cost={failureCost}>
            <div className="grid grid-cols-2 gap-2 md:gap-3">
              <InputField label="Failure Rate" value={failureRate} onChange={markChanged(setFailureRate)} suffix="%" icon={Percent} />
              <InputField label="Margin" value={margin} onChange={markChanged(setMargin)} suffix="%" icon={TrendingUp} />
            </div>
          </CostSection>
        </div>

        {/* Pricing breakdown */}
        <PricingBreakdown costs={costs} margin={margin} quantity={quantity} />
      </div>
    </div>
  )
}
