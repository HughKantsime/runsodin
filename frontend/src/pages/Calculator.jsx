import { useState, useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { models } from '../api'
import { 
  Calculator, Package, Clock, Zap, Printer, AlertTriangle,
  Box, DollarSign, Percent, TrendingUp
} from 'lucide-react'

function InputField({ label, value, onChange, suffix, icon: Icon, step = "any", min = 0 }) {
  return (
    <div>
      <label className="block text-sm text-farm-400 mb-1">{label}</label>
      <div className="relative">
        {Icon && (
          <Icon size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-farm-500" />
        )}
        <input
          type="number"
          step={step}
          min={min}
          value={value}
          onChange={(e) => onChange(parseFloat(e.target.value) || 0)}
          className={`w-full bg-farm-800 border border-farm-700 rounded-lg py-2 pr-12 ${Icon ? 'pl-9' : 'pl-3'}`}
        />
        {suffix && (
          <span className="absolute right-3 top-1/2 -translate-y-1/2 text-farm-500 text-sm">
            {suffix}
          </span>
        )}
      </div>
    </div>
  )
}

function CostSection({ title, icon: Icon, cost, children }) {
  return (
    <div className="bg-farm-900 rounded-xl border border-farm-800 p-4">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          {Icon && <Icon size={18} className="text-farm-400" />}
          <h3 className="font-medium">{title}</h3>
        </div>
        <span className="text-lg font-bold text-print-400">${cost.toFixed(2)}</span>
      </div>
      <div className="space-y-3">
        {children}
      </div>
    </div>
  )
}

function PricingBreakdown({ costs, margin, quantity }) {
  const subtotal = Object.values(costs).reduce((sum, c) => sum + c, 0)
  const withMargin = subtotal * (1 + margin / 100)
  const perUnit = withMargin
  const total = perUnit * quantity

  return (
    <div className="bg-farm-900 rounded-xl border border-farm-800 p-4 sticky top-4">
      <h3 className="font-display font-semibold mb-4">Final Pricing</h3>
      
      <div className="space-y-2 text-sm">
        <div className="flex justify-between">
          <span className="text-farm-400">Material Cost:</span>
          <span>${costs.material.toFixed(2)}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-farm-400">Labor Cost:</span>
          <span>${costs.labor.toFixed(2)}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-farm-400">Electricity:</span>
          <span>${costs.electricity.toFixed(2)}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-farm-400">Printer Depreciation:</span>
          <span>${costs.depreciation.toFixed(2)}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-farm-400">Packaging:</span>
          <span>${costs.packaging.toFixed(2)}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-farm-400">Failure Buffer ({costs.failureRate}%):</span>
          <span>${costs.failure.toFixed(2)}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-farm-400">Overhead:</span>
          <span>${costs.overhead.toFixed(2)}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-farm-400">Other Costs:</span>
          <span>${costs.other.toFixed(2)}</span>
        </div>
        
        <div className="border-t border-farm-700 pt-2 mt-2">
          <div className="flex justify-between font-medium">
            <span>Subtotal:</span>
            <span>${subtotal.toFixed(2)}</span>
          </div>
        </div>

        <div className="pt-2">
          <label className="block text-sm text-farm-400 mb-1">Manufacturing Margin</label>
          <div className="text-2xl font-bold text-center py-2">{margin}%</div>
        </div>

        <div className="bg-print-900/50 border border-print-700 rounded-lg p-4 text-center">
          <div className="text-sm text-farm-400">Recommended Price (per unit)</div>
          <div className="text-3xl font-display font-bold text-print-400">
            ${perUnit.toFixed(2)}
          </div>
        </div>

        {quantity > 1 && (
          <div className="bg-farm-800 rounded-lg p-3 text-center">
            <div className="text-sm text-farm-400">Total for {quantity} units</div>
            <div className="text-xl font-bold text-green-400">
              ${total.toFixed(2)}
            </div>
          </div>
        )}

        <div className="pt-2 space-y-1 text-xs text-farm-500">
          <div className="flex justify-between">
            <span>Cost per unit:</span>
            <span>${subtotal.toFixed(2)}</span>
          </div>
          <div className="flex justify-between">
            <span>Profit per unit:</span>
            <span className="text-green-400">${(withMargin - subtotal).toFixed(2)}</span>
          </div>
          <div className="flex justify-between">
            <span>Profit margin:</span>
            <span className="text-green-400">{((withMargin - subtotal) / withMargin * 100).toFixed(1)}%</span>
          </div>
        </div>
      </div>
    </div>
  )
}

export default function CalculatorPage() {
  const { data: modelsData } = useQuery({
    queryKey: ['models'],
    queryFn: models.list,
  })

  const [selectedModel, setSelectedModel] = useState('')
  const [quantity, setQuantity] = useState(1)

  // Material
  const [filamentGrams, setFilamentGrams] = useState(50)
  const [spoolCost, setSpoolCost] = useState(25)
  const [spoolWeight, setSpoolWeight] = useState(1000)

  // Labor
  const [hourlyRate, setHourlyRate] = useState(15)
  const [postProcessingMin, setPostProcessingMin] = useState(5)
  const [packingMin, setPackingMin] = useState(5)
  const [supportMin, setSupportMin] = useState(5)

  // Electricity
  const [electricityRate, setElectricityRate] = useState(0.12)
  const [printerWattage, setPrinterWattage] = useState(100)
  const [printTimeHours, setPrintTimeHours] = useState(2)

  // Depreciation
  const [printerCost, setPrinterCost] = useState(300)
  const [printerLifespan, setPrinterLifespan] = useState(5000)

  // Packaging
  const [packagingCost, setPackagingCost] = useState(0.45)

  // Failure Rate
  const [failureRate, setFailureRate] = useState(7)

  // Overhead
  const [monthlyRent, setMonthlyRent] = useState(0)
  const [partsPerMonth, setPartsPerMonth] = useState(100)

  // Other
  const [otherCosts, setOtherCosts] = useState(0)

  // Margin
  const [margin, setMargin] = useState(50)

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

  // Calculate costs
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
    other: otherCosts,
  }

  return (
    <div className="p-6">
      <div className="mb-6">
        <h1 className="text-2xl font-display font-bold flex items-center gap-2">
          <Calculator className="text-print-400" />
          Pricing Calculator
        </h1>
        <p className="text-farm-400">Calculate the true cost and recommended price for your prints</p>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Left column - inputs */}
        <div className="lg:col-span-2 space-y-4">
          {/* Model selector */}
          <div className="bg-farm-900 rounded-xl border border-farm-800 p-4">
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="block text-sm text-farm-400 mb-1">Select Model (optional)</label>
                <select
                  value={selectedModel}
                  onChange={(e) => setSelectedModel(e.target.value)}
                  className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2"
                >
                  <option value="">-- Manual Entry --</option>
                  {modelsData?.map(model => (
                    <option key={model.id} value={model.id}>{model.name}</option>
                  ))}
                </select>
              </div>
              <InputField
                label="Quantity"
                value={quantity}
                onChange={setQuantity}
                suffix="units"
                min={1}
                step={1}
              />
            </div>
          </div>

          {/* Material Costs */}
          <CostSection title="Material Costs" icon={Package} cost={materialCost}>
            <div className="grid grid-cols-3 gap-3">
              <InputField
                label="Filament Used"
                value={filamentGrams}
                onChange={setFilamentGrams}
                suffix="g"
              />
              <InputField
                label="Spool Cost"
                value={spoolCost}
                onChange={setSpoolCost}
                suffix="$"
              />
              <InputField
                label="Spool Weight"
                value={spoolWeight}
                onChange={setSpoolWeight}
                suffix="g"
              />
            </div>
            <div className="text-xs text-farm-500">
              Cost per gram: ${costPerGram.toFixed(3)}
            </div>
          </CostSection>

          {/* Labor Costs */}
          <CostSection title="Labor Costs" icon={Clock} cost={laborCost}>
            <InputField
              label="Your Hourly Rate"
              value={hourlyRate}
              onChange={setHourlyRate}
              suffix="$/hr"
            />
            <div className="grid grid-cols-3 gap-3">
              <InputField
                label="Post-Processing"
                value={postProcessingMin}
                onChange={setPostProcessingMin}
                suffix="min"
              />
              <InputField
                label="Packing Time"
                value={packingMin}
                onChange={setPackingMin}
                suffix="min"
              />
              <InputField
                label="Customer Support"
                value={supportMin}
                onChange={setSupportMin}
                suffix="min"
              />
            </div>
            <div className="text-xs text-farm-500">
              Total time: {postProcessingMin + packingMin + supportMin} min ({laborHours.toFixed(2)} hours)
            </div>
          </CostSection>

          {/* Electricity */}
          <CostSection title="Electricity" icon={Zap} cost={electricityCost}>
            <div className="grid grid-cols-3 gap-3">
              <InputField
                label="Electricity Rate"
                value={electricityRate}
                onChange={setElectricityRate}
                suffix="$/kWh"
              />
              <InputField
                label="Printer Wattage"
                value={printerWattage}
                onChange={setPrinterWattage}
                suffix="W"
              />
              <InputField
                label="Print Time"
                value={printTimeHours}
                onChange={setPrintTimeHours}
                suffix="hrs"
              />
            </div>
            <div className="text-xs text-farm-500">
              Energy consumption: {((printerWattage / 1000) * printTimeHours).toFixed(3)} kWh
            </div>
          </CostSection>

          {/* Printer Depreciation */}
          <CostSection title="Printer Depreciation" icon={Printer} cost={depreciationCost}>
            <div className="grid grid-cols-2 gap-3">
              <InputField
                label="Printer Cost"
                value={printerCost}
                onChange={setPrinterCost}
                suffix="$"
              />
              <InputField
                label="Expected Lifespan"
                value={printerLifespan}
                onChange={setPrinterLifespan}
                suffix="hrs"
              />
            </div>
            <div className="text-xs text-farm-500">
              Depreciation rate: ${(printerCost / printerLifespan).toFixed(4)}/hr
            </div>
          </CostSection>

          {/* Packaging & Failure */}
          <div className="grid grid-cols-2 gap-4">
            <CostSection title="Packaging" icon={Box} cost={packagingCost}>
              <InputField
                label="Box/Packaging Cost"
                value={packagingCost}
                onChange={setPackagingCost}
                suffix="$"
              />
            </CostSection>

            <CostSection title="Failure Rate" icon={AlertTriangle} cost={failureCost}>
              <InputField
                label="Failure Rate"
                value={failureRate}
                onChange={setFailureRate}
                suffix="%"
              />
              <div className="text-xs text-farm-500">
                Buffer for failed prints
              </div>
            </CostSection>
          </div>

          {/* Overhead & Other */}
          <div className="grid grid-cols-2 gap-4">
            <CostSection title="Rent & Overhead" icon={DollarSign} cost={overheadCost}>
              <InputField
                label="Monthly Rent/Overhead"
                value={monthlyRent}
                onChange={setMonthlyRent}
                suffix="$/mo"
              />
              <InputField
                label="Parts Per Month"
                value={partsPerMonth}
                onChange={setPartsPerMonth}
                suffix="parts"
              />
            </CostSection>

            <CostSection title="Other Costs" icon={Package} cost={otherCosts}>
              <InputField
                label="Screws, inserts, etc."
                value={otherCosts}
                onChange={setOtherCosts}
                suffix="$"
              />
            </CostSection>
          </div>

          {/* Margin */}
          <div className="bg-farm-900 rounded-xl border border-farm-800 p-4">
            <div className="flex items-center gap-2 mb-4">
              <TrendingUp size={18} className="text-farm-400" />
              <h3 className="font-medium">Manufacturing Margin</h3>
            </div>
            <div className="flex items-center gap-4">
              <input
                type="range"
                min="0"
                max="200"
                value={margin}
                onChange={(e) => setMargin(parseInt(e.target.value))}
                className="flex-1 accent-print-500"
              />
              <div className="w-20">
                <InputField
                  label=""
                  value={margin}
                  onChange={setMargin}
                  suffix="%"
                />
              </div>
            </div>
          </div>
        </div>

        {/* Right column - pricing breakdown */}
        <div>
          <PricingBreakdown costs={costs} margin={margin} quantity={quantity} />
        </div>
      </div>
    </div>
  )
}
