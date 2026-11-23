import { render, screen, fireEvent, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import cases from '../../../../tests/fixtures/library_item_pricing.json'
import { ModelCard } from './ModelLibraryFields'
import CalculatorPage from '../../pages/orders/Calculator'

const api = vi.hoisted(() => ({ get: vi.fn(), list: vi.fn() }))
vi.mock('../../api', () => ({ models: { list: api.list }, pricingConfig: { get: api.get } }))
vi.mock('../../permissions', () => ({ canDo: () => false }))

const actions = { onEdit: vi.fn(), onDelete: vi.fn(), onSchedule: vi.fn(), onToggleFavorite: vi.fn(), onView3D: vi.fn(), onRevisions: vi.fn() }
function mount(children: React.ReactNode) {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{children}</QueryClientProvider>)
}
afterEach(() => { cleanup(); delete document.documentElement.dataset.themeFamily; vi.clearAllMocks() })

// Backend HTTP tests pin these response values against /models-with-pricing and /models/{id}/cost.
for (const theme of ['classic', 'foundry']) {
  describe(`${theme} per-item library prices`, () => {
    for (const c of cases) {
      it(`${c.name}: card and calculator match the endpoint to the cent`, async () => {
        document.documentElement.dataset.themeFamily = theme
        api.get.mockResolvedValue({ ...c.config, configured: true })
        api.list.mockResolvedValue([c.model])
        mount(<><section aria-label="Model card"><ModelCard model={{ ...c.model, ...c.expected }} {...actions} /></section><CalculatorPage /></>)
        const card = within(screen.getByRole('region', { name: 'Model card' }))
        expect(card.getByText(/^Cost per item:/)).toHaveTextContent(`Cost per item: ${c.expected.estimated_cost.toFixed(2)}`)
        expect(card.getByText(/^Price per item:/)).toHaveTextContent(`Price per item: ${c.expected.suggested_price.toFixed(2)}`)
        if (c.model.units_per_bed > 1) expect(card.getByText(`${c.model.units_per_bed} per plate`)).toBeInTheDocument()
        else expect(card.queryByText(/per plate/)).toBeNull()
        await screen.findByText('Final Pricing')
        fireEvent.change(screen.getByLabelText('Select model'), { target: { value: String(c.model.id) } })
        // The calculator edits global markup; selecting a model currently imports only plate facts.
        fireEvent.change(screen.getByLabelText('Markup'), { target: { value: String(c.expected.effective_markup_percent) } })
        expect(screen.getByLabelText('Units per Bed')).toHaveValue(c.model.units_per_bed)
        expect(screen.getByTestId('recommended-price')).toHaveTextContent(`${c.expected.suggested_price.toFixed(2)}`)
        expect(screen.getByText('Subtotal:').parentElement).toHaveTextContent(`${c.expected.estimated_cost.toFixed(2)}`)
      })
    }

    it('keeps the plate count visible before pricing is configured, without showing prices', () => {
      document.documentElement.dataset.themeFamily = theme
      const c = cases[0]
      mount(<ModelCard model={{ ...c.model, ...c.expected }} {...actions} pricingConfigured={false} />)
      expect(screen.getByText('6 per plate')).toBeInTheDocument()
      expect(screen.queryByText(/Cost per item:/)).toBeNull()
      expect(screen.queryByText(/Price per item:/)).toBeNull()
    })
  })
}
