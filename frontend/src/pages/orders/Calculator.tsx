import { useMoney } from '../../contexts/DisplayCurrency'
import { DisplayCurrency } from '../../contexts/DisplayCurrency'
import PageState from '../../components/ui/PageState'
import { useState, useEffect, useContext, useId, type ReactNode } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import toast from 'react-hot-toast'
import { models, pricingConfig } from '../../api'
import { priceItem, marginFromMarkup, markupFromMargin, type PricedItem } from '../../utils/pricingRule'
import {
  type LucideIcon,
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

function InputField({ label, value, onChange, suffix, icon: Icon, step = "any", min = 0, max, hint }: { label: string; value: number; onChange: (value: number) => void; suffix?: string; icon?: LucideIcon; max?: number; hint?: string; step?: string | number; min?: number }) {
  const inputId = useId()
  return (
    <div>
      <label htmlFor={inputId} className="block text-xs md:text-sm text-[var(--brand-muted)] mb-1">{label}</label>
      <div className="relative">
        {Icon && <Icon size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-[var(--brand-muted)]" aria-hidden="true" />}
        <input
          id={inputId}
          type="number"
          step={step}
          min={min}
          max={max}
          value={value}
          onChange={(e) => onChange(parseFloat(e.target.value) || 0)}
          className={`w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md py-2 pr-10 text-sm ${Icon ? 'pl-8' : 'pl-3'}`}
        />
        {suffix && <span className="absolute right-3 top-1/2 -translate-y-1/2 text-[var(--brand-muted)] text-xs">{suffix}</span>}
      </div>
      {hint && <p className="text-[11px] text-[var(--brand-muted)] mt-1">{hint}</p>}
    </div>
  )
}

function CostSection({ title, icon: Icon, cost, children }: { title: string; icon?: LucideIcon; cost: number; children: ReactNode }) {
  const money = useMoney()
  return (
    <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-3 md:p-4">
      <div className="flex items-center justify-between mb-3 md:mb-4">
        <div className="flex items-center gap-2">
          {Icon && <Icon size={16} className="text-[var(--brand-muted)]" />}
          <h3 className="font-medium text-sm">{title}</h3>
        </div>
        <span className="text-base md:text-lg font-bold text-[var(--brand-primary)] font-mono">{money(cost)}</span>
      </div>
      <div className="space-y-3">{children}</div>
    </div>
  )
}

function PricingBreakdown({ priced, failureRate, quantity, unitsPerBed = 1 }: { priced: PricedItem; failureRate: number; quantity: number; unitsPerBed?: number }) {
  const money = useMoney()
  // `priced` is per sellable item (per-plate inputs were divided by the unit count).
  const units = Number.isFinite(unitsPerBed) && unitsPerBed >= 1 ? unitsPerBed : 1
  const perUnit = priced.price
  const profit = priced.price - priced.selling_fees - priced.subtotal
  const hasFees = priced.selling_fees > 0
  const row = (label: string, value: number) => (
    <div className="flex justify-between"><span className="text-[var(--brand-muted)] font-sans">{label}</span><span>{money(value)}</span></div>
  )

  return (
    <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-3 md:p-4 lg:sticky lg:top-4">
      <h3 className="font-display font-semibold mb-4 text-sm md:text-base">Final Pricing</h3>
      <div className="space-y-2 text-sm font-mono">
        {row('Material Cost:', priced.material)}
        {row('Labor Cost:', priced.labor)}
        {row('Electricity:', priced.electricity)}
        {row('Printer Depreciation:', priced.depreciation)}
        {row('Packaging:', priced.packaging)}
        {row(`Failure Buffer (${failureRate}%):`, priced.failure)}
        {row('Overhead:', priced.overhead)}
        {row('Other Costs:', priced.other)}

        <div className="border-t border-[var(--brand-card-border)] pt-2 mt-2">
          <div className="flex justify-between font-medium"><span>Subtotal:</span><span>{money(priced.subtotal)}</span></div>
        </div>

        <div className="pt-2">
          <div className="block text-sm text-[var(--brand-muted)] mb-1 font-sans">Markup <span className="text-xs">(added on top of cost)</span></div>
          <div className="text-2xl font-bold text-center py-2" data-testid="markup-percent">{Number(priced.markup_percent.toFixed(2))}%</div>
        </div>

        {hasFees && row('Selling fees:', priced.selling_fees)}

        {units > 1 && (
          <div className="flex justify-between text-xs text-[var(--brand-muted)]"><span>Bed price ({units} units per bed):</span><span>{money(perUnit * units)}</span></div>
        )}

        <div className="bg-[var(--brand-primary)]/10 border border-[var(--brand-primary)]/30 rounded-md p-3 md:p-4 text-center">
          <div className="text-xs md:text-sm text-[var(--brand-muted)]">Recommended Price (per unit)</div>
          <div className="text-2xl md:text-3xl font-display font-bold text-[var(--brand-primary)] font-mono" data-testid="recommended-price">{money(perUnit)}</div>
        </div>
        <div className="text-xs text-[var(--brand-muted)] text-center font-sans" data-testid="margin-of-price">
          Margin (of price): <span className="text-green-400 font-mono">{priced.margin_of_price_percent.toFixed(1)}%</span>{hasFees ? ' after selling fees' : ''}
        </div>

        {quantity > 1 && (
          <div className="bg-[var(--brand-input-bg)] rounded-md p-3 text-center">
            <div className="text-xs md:text-sm text-[var(--brand-muted)]">Total for {quantity} units</div>
            <div className="text-lg md:text-xl font-bold text-green-400 font-mono">{money(perUnit * quantity)}</div>
          </div>
        )}

        <div className="pt-2 space-y-1 text-xs text-[var(--brand-muted)]">
          <div className="flex justify-between"><span>Cost per unit:</span><span>{money(priced.subtotal)}</span></div>
          <div className="flex justify-between"><span>Profit per unit{hasFees ? ' (after fees)' : ''}:</span><span className="text-green-400">{money(profit)}</span></div>
        </div>
      </div>
    </div>
  )
}

export default function CalculatorPage() {
  const currency = useContext(DisplayCurrency)
  const money = useMoney()
  const queryClient = useQueryClient()
  
  // Fetch models for dropdown
  const { data: modelsData } = useQuery({ queryKey: ['models'], queryFn: () => models.list() })
  
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
      // The Models page reads the same settings under this key to decide whether to show prices.
      queryClient.invalidateQueries({ queryKey: ['pricing-config'] })
    },
    onError: (err: any) => toast.error('Save pricing config failed: ' + (err?.message || 'Unknown error')),
  })

  const [selectedModel, setSelectedModel] = useState('')
  const [quantity, setQuantity] = useState(1)
  const [unitsPerBed, setUnitsPerBed] = useState(1)
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
  const [margin, setMargin] = useState(50)          // stored as default_margin; it is a markup
  const [sellingFeePercent, setSellingFeePercent] = useState(0)
  const [sellingFeeFixed, setSellingFeeFixed] = useState(0)
  const [plateHandlingMin, setPlateHandlingMin] = useState(0)

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
      setSellingFeePercent(savedConfig.selling_fee_percent ?? 0)
      setSellingFeeFixed(savedConfig.selling_fee_fixed ?? 0)
      setPlateHandlingMin(savedConfig.plate_handling_min ?? 0)
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
        // Model grams/hours are per bed; price per unit divides by the bed's unit count.
        setUnitsPerBed(model.units_per_bed && model.units_per_bed > 0 ? model.units_per_bed : 1)
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
    if (!(sellingFeePercent >= 0 && sellingFeePercent < 100)) { toast.error('Selling fee must be under 100%'); return }
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
      default_margin: margin,
      selling_fee_percent: sellingFeePercent,
      selling_fee_fixed: sellingFeeFixed,
      plate_handling_min: plateHandlingMin
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
      setSellingFeePercent(savedConfig.selling_fee_percent ?? 0)
      setSellingFeeFixed(savedConfig.selling_fee_fixed ?? 0)
      setPlateHandlingMin(savedConfig.plate_handling_min ?? 0)
      setHasChanges(false)
    }
  }

  // Calculations, per sellable item, through the one shared rule (utils/pricingRule.ts, the
  // same rule the API uses). Filament grams and print time describe one plate, so they are
  // divided by the plate's unit count here; labour, packaging, overhead and other costs are
  // per item, and plate handling is shared between the plate's units inside the rule.
  const bedUnits = Number.isFinite(unitsPerBed) && unitsPerBed >= 1 ? unitsPerBed : 1
  const costPerGram = spoolCost / spoolWeight
  const feePercentValid = sellingFeePercent >= 0 && sellingFeePercent < 100
  const priced = priceItem({
    hourly_rate: hourlyRate, post_processing_min: postProcessingMin, packing_min: packingMin, support_min: supportMin,
    plate_handling_min: plateHandlingMin, electricity_rate: electricityRate, printer_wattage: printerWattage,
    printer_cost: printerCost, printer_lifespan: printerLifespan, packaging_cost: packagingCost, other_costs: otherCosts,
    failure_rate: failureRate, monthly_rent: monthlyRent, parts_per_month: partsPerMonth, default_margin: margin,
    selling_fee_percent: feePercentValid ? sellingFeePercent : 0, selling_fee_fixed: sellingFeeFixed,
  }, { material_cost: filamentGrams * costPerGram / bedUnits, print_hours: printTimeHours / bedUnits, units_per_plate: bedUnits })
  const materialCost = priced.material
  const laborCost = priced.labor
  const electricityCost = priced.electricity
  const depreciationCost = priced.depreciation
  const failureCost = priced.failure
  const overheadCost = priced.overhead
  const targetMargin = Number(marginFromMarkup(margin).toFixed(1))
  const setTargetMargin = (value: number) => {
    const markup = markupFromMargin(value)
    if (markup === null) { toast.error('Target margin must be 0 or more and under 100%'); return }
    setMargin(Number(markup.toFixed(2)))
    setHasChanges(true)
  }

  if (configLoading) {
    return (
      <div className="p-4 md:p-6 flex items-center justify-center min-h-[400px]">
        <PageState kind="loading" className="text-[var(--brand-muted)]">Loading pricing configuration...</PageState>
      </div>
    )
  }

  return (
    <div className="p-4 md:p-6">
      <div className="mb-4 md:mb-6">
        <div className="flex items-center justify-between">
          <div>
            <div className="flex items-center gap-3">
              <Calculator className="text-[var(--brand-primary)]" size={24} />
              <h1 className="text-xl md:text-2xl font-display font-bold">Pricing Calculator</h1>
            </div>
            <p className="text-[var(--brand-muted)] text-sm mt-1">Calculate the true cost and recommended price for your prints</p>
          </div>
          <div className="flex gap-2">
            {hasChanges && (
              <button
                onClick={handleResetConfig}
                className="flex items-center gap-2 px-3 py-2 bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md text-sm hover:bg-[var(--brand-card-border)]"
              >
                <RefreshCw size={16} />
                Reset
              </button>
            )}
            <button
              onClick={handleSaveConfig}
              disabled={!hasChanges || saveConfigMutation.isPending}
              className={`flex items-center gap-2 px-4 py-2 rounded-md text-sm font-medium transition-colors ${
                hasChanges 
                  ? 'bg-[var(--brand-primary)] hover:bg-[var(--brand-primary-hover)] text-white' 
                  : 'bg-[var(--brand-input-bg)] text-[var(--brand-muted)] cursor-not-allowed'
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
          <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-3 md:p-4">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 md:gap-4">
              <div>
                <label className="block text-xs md:text-sm text-[var(--brand-muted)] mb-1">Select Model (optional)</label>
                <select aria-label="Select model"
                  value={selectedModel}
                  onChange={(e) => setSelectedModel(e.target.value)}
                  className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-sm"
                >
                  <option value="">-- Manual Entry --</option>
                  {modelsData?.map(model => (
                    <option key={model.id} value={model.id}>{model.name}</option>
                  ))}
                </select>
              </div>
              <InputField label="Quantity" value={quantity} onChange={setQuantity} suffix="units" min={1} step={1} />
              <InputField label="Units per Bed" value={unitsPerBed} onChange={setUnitsPerBed} suffix="units" min={1} step={1} />
            </div>
          </div>

          {/* Material Costs */}
          <CostSection title="Material Costs" icon={Package} cost={materialCost}>
            <div className="grid grid-cols-3 gap-2 md:gap-3">
              <InputField label="Filament Used" value={filamentGrams} onChange={setFilamentGrams} suffix="g" />
              <InputField label="Spool Cost" value={spoolCost} onChange={markChanged(setSpoolCost)} suffix={currency ?? undefined} />
              <InputField label="Spool Weight" value={spoolWeight} onChange={markChanged(setSpoolWeight)} suffix="g" />
            </div>
            <div className="text-xs text-[var(--brand-muted)]">Cost per gram: {money(costPerGram, '—', 3)}</div>
          </CostSection>

          {/* Labor Costs */}
          <CostSection title="Labor Costs" icon={Clock} cost={laborCost}>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-2 md:gap-3">
              <InputField label="Hourly Rate" value={hourlyRate} onChange={markChanged(setHourlyRate)} suffix={currency ? currency + "/hr" : "per hour"} icon={DollarSign} />
              <InputField label="Post-Process" value={postProcessingMin} onChange={markChanged(setPostProcessingMin)} suffix="min" />
              <InputField label="Packing" value={packingMin} onChange={markChanged(setPackingMin)} suffix="min" />
              <InputField label="Support" value={supportMin} onChange={markChanged(setSupportMin)} suffix="min" />
            </div>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-2 md:gap-3">
              <InputField label="Plate handling" value={plateHandlingMin} onChange={markChanged(setPlateHandlingMin)} suffix="min" hint="per plate: clear it and start the next" />
            </div>
            <div className="text-xs text-[var(--brand-muted)]">Post-process, packing and support are minutes per item. Plate handling is minutes per plate, shared between the items on it.</div>
          </CostSection>

          {/* Electricity Costs */}
          <CostSection title="Electricity" icon={Zap} cost={electricityCost}>
            <div className="grid grid-cols-3 gap-2 md:gap-3">
              <InputField label="Rate" value={electricityRate} onChange={markChanged(setElectricityRate)} suffix={currency ? currency + "/kWh" : "per kWh"} />
              <InputField label="Printer Watts" value={printerWattage} onChange={markChanged(setPrinterWattage)} suffix="W" />
              <InputField label="Print Time" value={printTimeHours} onChange={setPrintTimeHours} suffix="hrs" />
            </div>
          </CostSection>

          {/* Printer Depreciation */}
          <CostSection title="Printer Depreciation" icon={Printer} cost={depreciationCost}>
            <div className="grid grid-cols-2 gap-2 md:gap-3">
              <InputField label="Printer Cost" value={printerCost} onChange={markChanged(setPrinterCost)} suffix={currency ?? undefined} />
              <InputField label="Lifespan" value={printerLifespan} onChange={markChanged(setPrinterLifespan)} suffix="hrs" />
            </div>
          </CostSection>

          {/* Other Costs */}
          <CostSection title="Other Costs" icon={Box} cost={packagingCost + overheadCost + otherCosts}>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-2 md:gap-3">
              <InputField label="Packaging" value={packagingCost} onChange={markChanged(setPackagingCost)} suffix={currency ?? undefined} />
              <InputField label="Monthly Rent" value={monthlyRent} onChange={markChanged(setMonthlyRent)} suffix={currency ?? undefined} />
              <InputField label="Parts/Month" value={partsPerMonth} onChange={markChanged(setPartsPerMonth)} suffix="pcs" />
              <InputField label="Other" value={otherCosts} onChange={markChanged(setOtherCosts)} suffix={currency ?? undefined} />
            </div>
          </CostSection>

          {/* Failure Buffer */}
          <CostSection title="Failure Buffer" icon={AlertTriangle} cost={failureCost}>
            <div className="grid grid-cols-2 gap-2 md:gap-3">
              <InputField label="Failure Rate" value={failureRate} onChange={markChanged(setFailureRate)} suffix="%" icon={Percent} />
            </div>
          </CostSection>

          {/* Markup and selling fees */}
          <CostSection title="Markup and Selling Fees" icon={TrendingUp} cost={priced.price - priced.subtotal}>
            <div className="grid grid-cols-2 gap-2 md:gap-3">
              <InputField label="Markup" value={margin} onChange={markChanged(setMargin)} suffix="%" icon={TrendingUp} hint="added on top of cost" />
              <InputField label="Target margin" value={targetMargin} onChange={setTargetMargin} suffix="%" icon={Percent} max={99.9} hint="the same setting, as a share of the price" />
              <InputField label="Selling fee" value={sellingFeePercent} onChange={markChanged(setSellingFeePercent)} suffix="%" icon={Percent} max={99.9} hint="card or marketplace share of the price" />
              <InputField label="Fixed fee" value={sellingFeeFixed} onChange={markChanged(setSellingFeeFixed)} suffix={currency ?? undefined} icon={DollarSign} hint="per sale" />
            </div>
            {!feePercentValid && <div role="alert" className="text-xs text-red-400">Selling fee must be under 100%.</div>}
            <div className="text-xs text-[var(--brand-muted)]">Markup and target margin are one setting: change either and the other follows. The price is raised so the selling fees are covered and your markup still applies to your cost.</div>
          </CostSection>
        </div>

        {/* Pricing breakdown */}
        <PricingBreakdown priced={priced} failureRate={failureRate} quantity={quantity} unitsPerBed={unitsPerBed} />
      </div>
    </div>
  )
}
