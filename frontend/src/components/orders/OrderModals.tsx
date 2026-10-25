import MeasuredFilamentCost from './MeasuredFilamentCost'
import ShowFulfillment from './ShowFulfillment'
import {getCurrentUser} from '../../permissions'
import { useState } from 'react'
import { Plus, Truck } from 'lucide-react'
import { Modal } from '../ui'

const PLATFORMS = ['etsy', 'amazon', 'ebay', 'direct', 'wholesale', 'show_ship_later', 'other']

export function CreateOrderModal({ isOpen, onClose, onSubmit, formData, setFormData, items, setItems, productList }: { isOpen: boolean; onClose: () => void; onSubmit: (e: React.FormEvent) => void; formData: any; setFormData: (d: any) => void; items: any[]; setItems: (items: any[]) => void; productList: any[] }) {
  if (!isOpen) return null

  const addItem = () => setItems([...items, { product_id: '', quantity: 1, unit_price: '' }])

  const updateItem = (index, field, value) => {
    const updated = [...items]
    updated[index][field] = value
    if (field === 'product_id' && value) {
      const product = productList.find(p => p.id === parseInt(value))
      updated[index].unit_price = product?.price ?? ''
    }
    setItems(updated)
  }

  const removeItem = (index) => setItems(items.filter((_, i) => i !== index))

  const calcItemsTotal = () => items.reduce((sum, item) => {
    const qty = parseInt(item.quantity) || 0
    const price = parseFloat(item.unit_price) || 0
    return sum + (qty * price)
  }, 0)

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="New Order" size="xl" mobileSheet={false}>
      <form onSubmit={onSubmit} className="space-y-4">
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div>
            <label className="block text-sm font-medium mb-1 text-[var(--brand-text)]">Order Number</label>
            <input
              type="text"
              value={formData.order_number}
              onChange={(e) => setFormData({ ...formData, order_number: e.target.value })}
              className="w-full rounded-md px-3 py-2 bg-[var(--brand-content-bg)] border border-[var(--brand-card-border)] text-[var(--brand-text)] focus:border-[var(--brand-primary)] focus:outline-hidden text-sm"
              placeholder="ETSY-12345"
            />
          </div>
          <div>
            <label className="block text-sm font-medium mb-1 text-[var(--brand-text)]">Platform</label>
            <select
              value={formData.platform}
              onChange={(e) => setFormData({ ...formData, platform: e.target.value })}
              className="w-full rounded-md px-3 py-2 bg-[var(--brand-content-bg)] border border-[var(--brand-card-border)] text-[var(--brand-text)] text-sm"
            >
              {PLATFORMS.map(p => (
                <option key={p} value={p}>{p.charAt(0).toUpperCase() + p.slice(1)}</option>
              ))}
            </select>
          </div>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div>
            <label className="block text-sm font-medium mb-1 text-[var(--brand-text)]">Customer Name</label>
            <input
              type="text"
              value={formData.customer_name}
              onChange={(e) => setFormData({ ...formData, customer_name: e.target.value })}
              className="w-full rounded-md px-3 py-2 bg-[var(--brand-content-bg)] border border-[var(--brand-card-border)] text-[var(--brand-text)] focus:border-[var(--brand-primary)] focus:outline-hidden text-sm"
            />
          </div>
          <div>
            <label className="block text-sm font-medium mb-1 text-[var(--brand-text)]">Customer Email</label>
            <input
              type="email"
              value={formData.customer_email}
              onChange={(e) => setFormData({ ...formData, customer_email: e.target.value })}
              className="w-full rounded-md px-3 py-2 bg-[var(--brand-content-bg)] border border-[var(--brand-card-border)] text-[var(--brand-text)] focus:border-[var(--brand-primary)] focus:outline-hidden text-sm"
            />
          </div>
        </div>

        {/* Line Items */}
        <div>
          <div className="flex justify-between items-center mb-2">
            <label className="block text-sm font-medium text-[var(--brand-text)]">Line Items</label>
            <button type="button" onClick={addItem} className="text-xs text-[var(--brand-primary)] hover:text-[var(--brand-primary)] flex items-center gap-1">
              <Plus size={14} /> Add Item
            </button>
          </div>
          {items.length === 0 ? (
            <p className="text-sm text-[var(--brand-muted)]">Add products to this order</p>
          ) : (
            <div className="space-y-2">
              {items.map((item, i) => (
                <div key={i} className="flex flex-col sm:flex-row gap-2 items-stretch sm:items-center">
                  <select
                    value={item.product_id}
                    onChange={(e) => updateItem(i, 'product_id', e.target.value)}
                    className="flex-1 rounded-md px-2 py-1.5 text-sm bg-[var(--brand-content-bg)] border border-[var(--brand-card-border)] text-[var(--brand-text)]"
                  >
                    <option value="">Select product...</option>
                    {productList.map(p => (
                      <option key={p.id} value={p.id}>{p.name} {p.sku ? `(${p.sku})` : ''}</option>
                    ))}
                  </select>
                  <div className="flex gap-2">
                    <input
                      type="number"
                      min="1"
                      value={item.quantity}
                      onChange={(e) => updateItem(i, 'quantity', e.target.value)}
                      className="w-16 rounded-md px-2 py-1.5 text-sm bg-[var(--brand-content-bg)] border border-[var(--brand-card-border)] text-[var(--brand-text)]"
                      placeholder="Qty"
                    />
                    <input
                      type="number"
                      step="0.01"
                      value={item.unit_price}
                      onChange={(e) => updateItem(i, 'unit_price', e.target.value)}
                      className="w-24 rounded-md px-2 py-1.5 text-sm bg-[var(--brand-content-bg)] border border-[var(--brand-card-border)] text-[var(--brand-text)]"
                      placeholder="Price"
                    />
                    <button
                      type="button"
                      onClick={() => removeItem(i)}
                      className="p-2 text-[var(--brand-muted)] hover:text-red-400 hover:bg-red-900/30 rounded-md transition-colors"
                    >
                      <span className="sr-only">Remove item</span>&times;
                    </button>
                  </div>
                </div>
              ))}
              <div className="text-right text-sm text-[var(--brand-muted)] font-mono">
                Subtotal: ${calcItemsTotal().toFixed(2)}
              </div>
            </div>
          )}
        </div>

        {/* Financials */}
        <div className="border-t border-[var(--brand-card-border)] pt-4">
          <label className="block text-sm font-medium mb-2 text-[var(--brand-text)]">Financials</label>
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-4">
            <div>
              <label className="block text-xs mb-1 text-[var(--brand-muted)]">Revenue</label>
              <input type="number" step="0.01" value={formData.revenue} onChange={(e) => setFormData({ ...formData, revenue: e.target.value })} className="w-full rounded-md px-3 py-2 bg-[var(--brand-content-bg)] border border-[var(--brand-card-border)] text-[var(--brand-text)] text-sm" placeholder="0.00" />
            </div>
            <div>
              <label className="block text-xs mb-1 text-[var(--brand-muted)]">Platform Fees</label>
              <input type="number" step="0.01" value={formData.platform_fees} onChange={(e) => setFormData({ ...formData, platform_fees: e.target.value })} className="w-full rounded-md px-3 py-2 bg-[var(--brand-content-bg)] border border-[var(--brand-card-border)] text-[var(--brand-text)] text-sm" placeholder="0.00" />
            </div>
            <div>
              <label className="block text-xs mb-1 text-[var(--brand-muted)]">Payment Fees</label>
              <input type="number" step="0.01" value={formData.payment_fees} onChange={(e) => setFormData({ ...formData, payment_fees: e.target.value })} className="w-full rounded-md px-3 py-2 bg-[var(--brand-content-bg)] border border-[var(--brand-card-border)] text-[var(--brand-text)] text-sm" placeholder="0.00" />
            </div>
            <div>
              <label className="block text-xs mb-1 text-[var(--brand-muted)]">Shipping Charged</label>
              <input type="number" step="0.01" value={formData.shipping_charged} onChange={(e) => setFormData({ ...formData, shipping_charged: e.target.value })} className="w-full rounded-md px-3 py-2 bg-[var(--brand-content-bg)] border border-[var(--brand-card-border)] text-[var(--brand-text)] text-sm" placeholder="0.00" />
            </div>
            <div>
              <label className="block text-xs mb-1 text-[var(--brand-muted)]">Shipping Cost</label>
              <input type="number" step="0.01" value={formData.shipping_cost} onChange={(e) => setFormData({ ...formData, shipping_cost: e.target.value })} className="w-full rounded-md px-3 py-2 bg-[var(--brand-content-bg)] border border-[var(--brand-card-border)] text-[var(--brand-text)] text-sm" placeholder="0.00" />
            </div>
          </div>
        </div>

        <div>
          <label className="block text-sm font-medium mb-1 text-[var(--brand-text)]">Notes</label>
          <textarea value={formData.notes} onChange={(e) => setFormData({ ...formData, notes: e.target.value })} className="w-full rounded-md px-3 py-2 bg-[var(--brand-content-bg)] border border-[var(--brand-card-border)] text-[var(--brand-text)] text-sm" rows={2} />
        </div>

        <div className="flex justify-end gap-2 pt-4 border-t border-[var(--brand-card-border)]">
          <button type="button" onClick={onClose} className="px-4 py-2 bg-[var(--brand-input-bg)] hover:bg-[var(--brand-card-border)] rounded-md transition-colors text-sm">
            Cancel
          </button>
          <button type="submit" className="px-4 py-2 bg-[var(--brand-primary)] hover:bg-[var(--brand-primary-hover)] rounded-md transition-colors text-sm">
            Create Order
          </button>
        </div>
      </form>
    </Modal>
  )
}

export function ProductionConfirmation({ item, onConfirm }: { item: any; onConfirm: (itemId: number, quantity: number) => Promise<void> }) {
  const [quantity, setQuantity] = useState(String(item.fulfilled_quantity ?? 0))
  const [busy, setBusy] = useState(false)
  const amount = Number(quantity)
  const valid = quantity.trim() !== '' && Number.isInteger(amount) && amount >= 0 && amount <= item.quantity
  return (
    <form className="flex flex-wrap items-center gap-2 text-sm" onSubmit={async event => {
      event.preventDefault()
      if (!valid || busy) return
      setBusy(true)
      try { await onConfirm(item.id, amount) } catch { /* The caller displays the server error. */ }
      finally { setBusy(false) }
    }}>
      <label htmlFor={`production-${item.id}`} className="text-[var(--brand-muted)]">Produced quantity</label>
      <input id={`production-${item.id}`} type="number" min={0} max={item.quantity} step={1}
        value={quantity} onChange={event => setQuantity(event.target.value)} disabled={busy}
        className="w-20 rounded-md px-2 py-1 bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)]" />
      <button type="submit" disabled={!valid || busy}
        className="px-3 py-1 rounded-md bg-[var(--brand-primary)] disabled:opacity-50">
        {busy ? 'Saving…' : 'Confirm production'}
      </button>
    </form>
  )
}

const finiteMoney = (value) => typeof value === 'number' && Number.isFinite(value)
  ? `${value < 0 ? '-' : ''}$${Math.abs(value).toFixed(2)}` : 'Unknown'

export function OrderDetailModal({ isOpen, order, onClose, onSchedule, onShip, onCancel, onDownloadInvoice, onConfirmProduction, onShowUpdated, STATUS_CLASSES, canDo }: { isOpen: boolean; order: any; onClose: () => void; onSchedule: (id: number) => void; onShip: (order: any) => void; onCancel: (order: any) => void; onDownloadInvoice: (order: any) => void; onConfirmProduction: (itemId: number, quantity: number) => Promise<void>; onShowUpdated?: (id:number)=>void; STATUS_CLASSES: Record<string, string>; canDo: (action: string) => boolean }) {
  if (!isOpen || !order) return null

  return (
    <Modal isOpen={isOpen} onClose={onClose} title={`Order ${order.order_number || `#${order.id}`}`} size="xl" mobileSheet={false}>
      <div className="space-y-4">
        <MeasuredFilamentCost key={order.id} orderId={order.id} />
        {/* Status & Actions */}
        <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-2">
          <span className={`px-3 py-1 rounded-md text-sm ${STATUS_CLASSES[order.status] || 'bg-[var(--brand-card-border)] text-[var(--brand-text-secondary)]'}`}>
            {order.status?.replace('_', ' ')}
          </span>
          <div className="flex gap-2">
            {canDo('orders.edit') && order.status === 'pending' && (
              <button
                onClick={() => onSchedule(order.id)}
                className="px-4 py-2 bg-[var(--brand-primary)] hover:bg-[var(--brand-primary-hover)] rounded-md transition-colors text-sm"
              >
                Schedule Jobs
              </button>
            )}
            {canDo('orders.ship') && !order.has_show_fulfillment && order.status === 'fulfilled' && (
              <button
                onClick={() => onShip(order)}
                className="px-4 py-2 bg-[var(--brand-primary)] hover:bg-[var(--brand-primary-hover)] rounded-md transition-colors text-sm"
              >
                Mark Shipped
              </button>
            )}
            {canDo('orders.edit') && (order.status === 'pending' || order.status === 'in_progress') && (
              <button
                onClick={() => onCancel(order)}
                className="px-4 py-2 bg-red-600 hover:bg-red-500 rounded-md transition-colors text-sm flex items-center gap-2"
              >
                Cancel Order
              </button>
            )}
            <button
              onClick={() => onDownloadInvoice(order)}
              className="px-4 py-2 bg-[var(--brand-input-bg)] hover:bg-[var(--brand-card-border)] rounded-md transition-colors text-sm flex items-center gap-2"
              title="Download Invoice PDF"
            >
              Invoice
            </button>
          </div>
        </div>

        {/* Customer Info */}
        <div className="grid grid-cols-2 gap-4 text-sm">
          <div><span className="text-[var(--brand-muted)]">Platform:</span> <span className="text-[var(--brand-text)]">{order.platform || '-'}</span></div>
          <div><span className="text-[var(--brand-muted)]">Customer:</span> <span className="text-[var(--brand-text)]">{order.customer_name || '-'}</span></div>
        </div>

        {/* Line Items */}
        <div>
          <h3 className="font-medium mb-2 text-[var(--brand-text)]">Items</h3>
          {order.items?.some(item => item.can_confirm_production) && (
            <p className="text-sm text-[var(--brand-muted)] mb-2">Confirm quantities produced outside scheduled printing. This records production, separate from shipping.</p>
          )}
          <div className="rounded-md p-3 space-y-2 bg-[var(--brand-content-bg)]">
            {order.items?.map(item => (
              <div key={item.id} className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-1">
                <div className="text-[var(--brand-text)]">
                  <span className="font-medium">{item.quantity}x</span> {item.product_name || `Product #${item.product_id}`}
                  {item.product_sku && <span className="text-sm ml-2 text-[var(--brand-muted)]">({item.product_sku})</span>}
                </div>
                {item.can_confirm_production && canDo('orders.ship') && (
                  <ProductionConfirmation key={`${order.id}-${item.id}-${item.fulfilled_quantity}`} item={item} onConfirm={onConfirmProduction} />
                )}
                <div className="flex items-center gap-4 text-sm">
                  <span className="text-[var(--brand-text-secondary)]">{item.subtotal == null ? 'Not recorded' : `$${item.subtotal.toFixed(2)}`}</span>
                  <span className={item.is_fulfilled ? 'text-status-printing' : 'text-status-pending'}>
                    {item.is_fulfilled ? 'Done' : `${item.fulfilled_quantity}/${item.quantity}`}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Jobs Progress */}
        <div>
          <h3 className="font-medium mb-2 text-[var(--brand-text)]">Jobs</h3>
          <div className="text-sm text-[var(--brand-text-secondary)]">
            {order.jobs_complete} / {order.jobs_total} complete
            <div
              className="w-full rounded-md h-2 mt-1 bg-[var(--brand-input-bg)]"
              role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-label="Jobs complete"
              aria-valuenow={order.jobs_total ? Math.round((order.jobs_complete / order.jobs_total) * 100) : 0}
            >
              <div
                className="h-2 rounded-md bg-[var(--brand-primary)]"
                style={{ width: `${order.jobs_total ? (order.jobs_complete / order.jobs_total) * 100 : 0}%` }}
              />
            </div>
          </div>
        </div>

        {/* P&L */}
        <div>
          <h3 className="font-medium mb-2 text-[var(--brand-text)]">Profit & Loss</h3>
          <div className="rounded-md p-3 space-y-1 text-sm font-mono bg-[var(--brand-content-bg)]">
            <div className="flex justify-between">
              <span className="text-[var(--brand-muted)] font-sans">Revenue</span>
              <span className="font-medium text-[var(--brand-text)]">{order.revenue == null ? 'Not recorded' : `$${order.revenue.toFixed(2)}`}</span>
            </div>
            {order.platform_fees > 0 && (
              <div className="flex justify-between text-[var(--brand-muted)]">
                <span>Platform Fees</span>
                <span>-${order.platform_fees.toFixed(2)}</span>
              </div>
            )}
            {order.payment_fees > 0 && (
              <div className="flex justify-between text-[var(--brand-muted)]">
                <span>Payment Fees</span>
                <span>-${order.payment_fees.toFixed(2)}</span>
              </div>
            )}
            {order.shipping_cost > 0 && (
              <div className="flex justify-between text-[var(--brand-muted)]">
                <span>Shipping Cost</span>
                <span>-${order.shipping_cost.toFixed(2)}</span>
              </div>
            )}
            <div className="flex justify-between text-[var(--brand-muted)]">
              <span>Estimated Cost</span>
              <span>{typeof order.estimated_cost === 'number' && Number.isFinite(order.estimated_cost) ? `$${order.estimated_cost.toFixed(2)}` : 'Unknown'}</span>
            </div>
            <div className="flex justify-between text-[var(--brand-muted)]">
              <span>Actual Cost</span>
              <span>{typeof order.actual_cost === 'number' && Number.isFinite(order.actual_cost) ? `$${order.actual_cost.toFixed(2)}` : 'Unknown'}</span>
            </div>
            <div className="flex justify-between font-medium pt-1 mt-1 border-t border-[var(--brand-card-border)]">
              <span className="text-[var(--brand-text)]">Profit</span>
              <span>{typeof order.profit === 'number' && Number.isFinite(order.profit) ? `$${order.profit.toFixed(2)}` : 'Unknown'}</span>
            </div>
            <div className="flex justify-between text-[var(--brand-muted)]">
              <span>Margin</span>
              <span>{typeof order.margin_percent === 'number' && Number.isFinite(order.margin_percent) ? `${order.margin_percent}%` : 'Unknown'}</span>
            </div>
            {/* BOM estimates: filament + consumables per component, labeled as estimates. */}
            <div className="flex justify-between text-[var(--brand-muted)] pt-1 mt-1 border-t border-[var(--brand-card-border)]">
              <span>Estimated COGS (BOM)</span>
              <span>{finiteMoney(order.estimated_cogs)}</span>
            </div>
            <div className="flex justify-between text-[var(--brand-muted)]">
              <span>Est. Profit</span>
              <span>{finiteMoney(order.estimated_profit)}</span>
            </div>
            <div className="flex justify-between text-[var(--brand-muted)]">
              <span>Est. Margin</span>
              <span>{typeof order.estimated_margin_percent === 'number' && Number.isFinite(order.estimated_margin_percent) ? `${order.estimated_margin_percent}%` : 'Unknown'}</span>
            </div>
          </div>
        </div>

        {['operator','admin'].includes(getCurrentUser()?.role??'')&&<ShowFulfillment key={order.id} orderId={order.id} canEdit={canDo('orders.edit')} canShip={canDo('orders.ship')} onUpdated={onShowUpdated}/>}
        {order.tracking_number && (
          <div className="text-sm">
            <span className="text-[var(--brand-muted)]">Tracking:</span>{' '}
            <span className="text-[var(--brand-text)]">{order.tracking_number}</span>
          </div>
        )}
      </div>
    </Modal>
  )
}

export function ShippingModal({ isOpen, order, shippingForm, setShippingForm, onClose, onSubmit, isSubmitting = false }: { isOpen: boolean; order: any; shippingForm: any; setShippingForm: (fn: any) => void; onClose: () => void; onSubmit: () => void; isSubmitting?: boolean }) {
  if (!isOpen || !order) return null

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="Mark as Shipped" size="md" mobileSheet={false}>
      <div className="space-y-4">
        <div>
          <label className="block text-sm font-medium mb-1 text-[var(--brand-text)]">Tracking Number</label>
          <input
            type="text"
            disabled={isSubmitting}
            value={shippingForm.tracking_number}
            onChange={(e) => setShippingForm(f => ({ ...f, tracking_number: e.target.value }))}
            className="w-full rounded-md px-3 py-2 bg-[var(--brand-content-bg)] border border-[var(--brand-card-border)] text-[var(--brand-text)] text-sm"
            placeholder="Enter tracking number (optional)"
          />
        </div>
        <div>
          <label className="block text-sm font-medium mb-1 text-[var(--brand-text)]">Carrier</label>
          <select
            disabled={isSubmitting}
            value={shippingForm.carrier}
            onChange={(e) => setShippingForm(f => ({ ...f, carrier: e.target.value }))}
            className="w-full rounded-md px-3 py-2 bg-[var(--brand-content-bg)] border border-[var(--brand-card-border)] text-[var(--brand-text)] text-sm"
          >
            <option value="">Select carrier (optional)</option>
            <option value="usps">USPS</option>
            <option value="ups">UPS</option>
            <option value="fedex">FedEx</option>
            <option value="dhl">DHL</option>
            <option value="other">Other</option>
          </select>
        </div>
        <div className="flex justify-end gap-2 pt-2">
          <button disabled={isSubmitting} onClick={onClose} className="px-4 py-2 bg-[var(--brand-input-bg)] hover:bg-[var(--brand-card-border)] rounded-md transition-colors text-sm">Cancel</button>
          <button disabled={isSubmitting} onClick={onSubmit} className="px-4 py-2 bg-[var(--brand-primary)] hover:bg-[var(--brand-primary-hover)] rounded-md transition-colors text-sm flex items-center gap-2">
            <Truck size={14} /> Mark Shipped
          </button>
        </div>
      </div>
    </Modal>
  )
}

export function EditOrderModal({ isOpen, order, onClose, onSubmit, editFormData, setEditFormData, editItems, setEditItems, productList }: { isOpen: boolean; order: any; onClose: () => void; onSubmit: (e: React.FormEvent) => void; editFormData: any; setEditFormData: (fn: any) => void; editItems: any[]; setEditItems: (items: any[]) => void; productList: any[] }) {
  if (!isOpen || !order) return null

  return (
    <Modal isOpen={isOpen} onClose={onClose} title={`Edit Order ${order.order_number || `#${order.id}`}`} size="xl" mobileSheet={false}>
      <form onSubmit={onSubmit} className="space-y-4">
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div>
            <label className="block text-sm font-medium mb-1 text-[var(--brand-text)]">Order Number</label>
            <input type="text" value={editFormData.order_number} onChange={(e) => setEditFormData(d => ({ ...d, order_number: e.target.value }))} className="w-full rounded-md px-3 py-2 bg-[var(--brand-content-bg)] border border-[var(--brand-card-border)] text-[var(--brand-text)] text-sm" />
          </div>
          <div>
            <label className="block text-sm font-medium mb-1 text-[var(--brand-text)]">Platform</label>
            <select value={editFormData.platform} onChange={(e) => setEditFormData(d => ({ ...d, platform: e.target.value }))} className="w-full rounded-md px-3 py-2 bg-[var(--brand-content-bg)] border border-[var(--brand-card-border)] text-[var(--brand-text)] text-sm">
              {PLATFORMS.map(p => (
                <option key={p} value={p}>{p.charAt(0).toUpperCase() + p.slice(1)}</option>
              ))}
            </select>
          </div>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div>
            <label className="block text-sm font-medium mb-1 text-[var(--brand-text)]">Customer Name</label>
            <input type="text" value={editFormData.customer_name} onChange={(e) => setEditFormData(d => ({ ...d, customer_name: e.target.value }))} className="w-full rounded-md px-3 py-2 bg-[var(--brand-content-bg)] border border-[var(--brand-card-border)] text-[var(--brand-text)] text-sm" />
          </div>
          <div>
            <label className="block text-sm font-medium mb-1 text-[var(--brand-text)]">Customer Email</label>
            <input type="email" value={editFormData.customer_email} onChange={(e) => setEditFormData(d => ({ ...d, customer_email: e.target.value }))} className="w-full rounded-md px-3 py-2 bg-[var(--brand-content-bg)] border border-[var(--brand-card-border)] text-[var(--brand-text)] text-sm" />
          </div>
        </div>
        <div>
          <label className="block text-sm font-medium mb-1 text-[var(--brand-text)]">Tracking Number</label>
          <input type="text" value={editFormData.tracking_number} onChange={(e) => setEditFormData(d => ({ ...d, tracking_number: e.target.value }))} className="w-full rounded-md px-3 py-2 bg-[var(--brand-content-bg)] border border-[var(--brand-card-border)] text-[var(--brand-text)] text-sm" placeholder="Optional" />
        </div>
        <div className="border-t border-[var(--brand-card-border)] pt-4">
          <label className="block text-sm font-medium mb-2 text-[var(--brand-text)]">Financials</label>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
            <div>
              <label className="block text-xs mb-1 text-[var(--brand-muted)]">Revenue</label>
              <input type="number" step="0.01" value={editFormData.revenue} onChange={(e) => setEditFormData(d => ({ ...d, revenue: e.target.value }))} className="w-full rounded-md px-3 py-2 bg-[var(--brand-content-bg)] border border-[var(--brand-card-border)] text-[var(--brand-text)] text-sm" />
            </div>
            <div>
              <label className="block text-xs mb-1 text-[var(--brand-muted)]">Platform Fees</label>
              <input type="number" step="0.01" value={editFormData.platform_fees} onChange={(e) => setEditFormData(d => ({ ...d, platform_fees: e.target.value }))} className="w-full rounded-md px-3 py-2 bg-[var(--brand-content-bg)] border border-[var(--brand-card-border)] text-[var(--brand-text)] text-sm" />
            </div>
            <div>
              <label className="block text-xs mb-1 text-[var(--brand-muted)]">Payment Fees</label>
              <input type="number" step="0.01" value={editFormData.payment_fees} onChange={(e) => setEditFormData(d => ({ ...d, payment_fees: e.target.value }))} className="w-full rounded-md px-3 py-2 bg-[var(--brand-content-bg)] border border-[var(--brand-card-border)] text-[var(--brand-text)] text-sm" />
            </div>
            <div>
              <label className="block text-xs mb-1 text-[var(--brand-muted)]">Shipping Charged</label>
              <input type="number" step="0.01" value={editFormData.shipping_charged} onChange={(e) => setEditFormData(d => ({ ...d, shipping_charged: e.target.value }))} className="w-full rounded-md px-3 py-2 bg-[var(--brand-content-bg)] border border-[var(--brand-card-border)] text-[var(--brand-text)] text-sm" />
            </div>
          </div>
        </div>
        <div>
          <label className="block text-sm font-medium mb-1 text-[var(--brand-text)]">Notes</label>
          <textarea value={editFormData.notes} onChange={(e) => setEditFormData(d => ({ ...d, notes: e.target.value }))} className="w-full rounded-md px-3 py-2 bg-[var(--brand-content-bg)] border border-[var(--brand-card-border)] text-[var(--brand-text)] text-sm" rows={2} />
        </div>

        {/* Line Items */}
        <div className="border-t border-[var(--brand-card-border)] pt-4">
          <div className="flex justify-between items-center mb-2">
            <label className="block text-sm font-medium text-[var(--brand-text)]">Line Items</label>
            <button type="button" onClick={() => setEditItems([...editItems, { product_id: '', quantity: 1, unit_price: '' }])} className="text-xs text-[var(--brand-primary)] hover:text-[var(--brand-primary)] flex items-center gap-1">
              <Plus size={14} /> Add Item
            </button>
          </div>
          {editItems.length === 0 ? (
            <p className="text-sm text-[var(--brand-muted)]">No line items</p>
          ) : (
            <div className="space-y-2">
              {editItems.map((item, i) => (
                <div key={item.id || `new-${i}`} className="flex flex-col sm:flex-row gap-2 items-stretch sm:items-center">
                  <select
                    value={item.product_id}
                    onChange={(e) => {
                      const updated = [...editItems]
                      updated[i].product_id = e.target.value
                      if (e.target.value) {
                        const product = productList.find(p => p.id === parseInt(e.target.value))
                        updated[i].unit_price = product?.price ?? ''
                      }
                      setEditItems(updated)
                    }}
                    className="flex-1 rounded-md px-2 py-1.5 text-sm bg-[var(--brand-content-bg)] border border-[var(--brand-card-border)] text-[var(--brand-text)]"
                    disabled={!!item.id}
                  >
                    <option value="">Select product...</option>
                    {productList.map(p => (
                      <option key={p.id} value={p.id}>{p.name} {p.sku ? `(${p.sku})` : ''}</option>
                    ))}
                  </select>
                  <div className="flex gap-2">
                    <input
                      type="number"
                      min="1"
                      value={item.quantity}
                      onChange={(e) => { const updated = [...editItems]; updated[i].quantity = e.target.value; setEditItems(updated) }}
                      className="w-16 rounded-md px-2 py-1.5 text-sm bg-[var(--brand-content-bg)] border border-[var(--brand-card-border)] text-[var(--brand-text)]"
                      placeholder="Qty"
                    />
                    <input
                      type="number"
                      step="0.01"
                      value={item.unit_price}
                      onChange={(e) => { const updated = [...editItems]; updated[i].unit_price = e.target.value; setEditItems(updated) }}
                      className="w-24 rounded-md px-2 py-1.5 text-sm bg-[var(--brand-content-bg)] border border-[var(--brand-card-border)] text-[var(--brand-text)]"
                      placeholder="Price"
                    />
                    <button
                      type="button"
                      onClick={() => setEditItems(editItems.filter((_, j) => j !== i))}
                      className="p-2 text-[var(--brand-muted)] hover:text-red-400 hover:bg-red-900/30 rounded-md transition-colors"
                    >
                      <span className="sr-only">Remove item</span>&times;
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="flex justify-end gap-2 pt-4 border-t border-[var(--brand-card-border)]">
          <button type="button" onClick={onClose} className="px-4 py-2 bg-[var(--brand-input-bg)] hover:bg-[var(--brand-card-border)] rounded-md transition-colors text-sm">Cancel</button>
          <button type="submit" className="px-4 py-2 bg-[var(--brand-primary)] hover:bg-[var(--brand-primary-hover)] rounded-md transition-colors text-sm">Save Changes</button>
        </div>
      </form>
    </Modal>
  )
}
