import { useState, useEffect, useRef } from 'react'
import { orders, products, orderInvoice } from '../../api'
import { ShoppingCart, Plus, RefreshCw, Clock, TrendingUp, Package, Truck, DollarSign } from 'lucide-react'
import { canDo } from '../../permissions'
import toast from 'react-hot-toast'
import ConfirmModal from '../../components/shared/ConfirmModal'
import { CreateOrderModal, OrderDetailModal, ShippingModal, EditOrderModal } from '../../components/orders/OrderModals'
import OrderTable from '../../components/orders/OrderTable'
import { PageHeader, Button, SearchInput, StatCard, EmptyState } from '../../components/ui'

const STATUS_CLASSES = {
  pending: 'bg-status-pending/20 text-status-pending',
  in_progress: 'bg-status-scheduled/20 text-status-scheduled',
  partial: 'bg-purple-500/20 text-purple-400',
  fulfilled: 'bg-status-printing/20 text-status-printing',
  shipped: 'bg-status-completed/20 text-status-completed',
  cancelled: 'bg-status-failed/20 text-status-failed',
}

const EMPTY_ORDER_FORM = { order_number: '', platform: 'etsy', customer_name: '', customer_email: '', revenue: '', platform_fees: '', payment_fees: '', shipping_charged: '', shipping_cost: '', notes: '' }

const optionalAmount = (value) => {
  if (value == null || String(value).trim() === '') return null
  const amount = Number(value)
  if (!Number.isFinite(amount)) throw new Error('Enter a finite amount')
  return amount
}

function OrderStatsBar({ orders: ordersList }) {
  if (!ordersList.length) return null
  const knownRevenue = ordersList.every(order => typeof order.revenue === 'number' && Number.isFinite(order.revenue))
  const revenueTotal = knownRevenue ? ordersList.reduce((sum, order) => sum + order.revenue, 0) : null
  // Revenue-weighted estimate: sum of BOM-estimated profit over sum of revenue, for
  // non-cancelled orders whose revenue and estimate are both known.
  const finite = (value) => typeof value === 'number' && Number.isFinite(value)
  const marginEligible = ordersList.filter(order => order.status !== 'cancelled')
  const marginKnown = marginEligible.filter(order => finite(order.revenue) && order.revenue > 0 && finite(order.estimated_profit))
  const weightedRevenue = marginKnown.reduce((sum, order) => sum + order.revenue, 0)
  const weightedProfit = marginKnown.reduce((sum, order) => sum + order.estimated_profit, 0)
  const weightedMargin = marginKnown.length && Number.isFinite(weightedRevenue) && Number.isFinite(weightedProfit) && weightedRevenue > 0
    ? (weightedProfit / weightedRevenue) * 100 : null
  const averageMargin = weightedMargin != null && Number.isFinite(weightedMargin) ? `${weightedMargin.toFixed(1)}%` : 'Unknown'
  const marginSubtitle = `Revenue-weighted estimate · ${marginKnown.length} of ${marginEligible.length} orders`
  const revenue = revenueTotal != null && Number.isFinite(revenueTotal) ? `$${revenueTotal.toFixed(2)}` : 'Unknown'
  return (
    <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-2 md:gap-3 mb-6">
      <StatCard label="Total Orders" value={ordersList.length} icon={Package} color="default" />
      <StatCard label="Pending" value={ordersList.filter(o => o.status === 'pending').length} icon={Clock} color="amber" />
      <StatCard label="In Progress" value={ordersList.filter(o => o.status === 'in_progress').length} icon={TrendingUp} color="blue" />
      <StatCard label="Fulfilled" value={ordersList.filter(o => o.status === 'fulfilled').length} icon={Package} color="green" />
      <StatCard label="Shipped" value={ordersList.filter(o => o.status === 'shipped').length} icon={Truck} color="purple" />
      <StatCard label="Revenue" value={revenue} icon={DollarSign} color="green" />
      <StatCard label="Average Margin" value={averageMargin} subtitle={marginSubtitle} icon={TrendingUp} color="default" />
    </div>
  )
}

export default function Orders() {
  const [orderList, setOrderList] = useState([])
  const [productList, setProductList] = useState([])
  const [loading, setLoading] = useState(true)
  const [statusFilter, setStatusFilter] = useState('')
  const [showModal, setShowModal] = useState(false)
  const [showDetailModal, setShowDetailModal] = useState(false)
  const [selectedOrder, setSelectedOrder] = useState(null)
  const [formData, setFormData] = useState(EMPTY_ORDER_FORM)
  const [items, setItems] = useState([])
  const [editingOrder, setEditingOrder] = useState(null)
  const editRequest = useRef(0)
  const closeEdit = () => { editRequest.current += 1; setEditingOrder(null) }
  useEffect(() => () => { editRequest.current += 1 }, [])
  const [editFormData, setEditFormData] = useState({})
  const [editItems, setEditItems] = useState([])
  const [searchQuery, setSearchQuery] = useState('')
  const [confirmAction, setConfirmAction] = useState(null)
  const [shippingOrder, setShippingOrder] = useState(null)
  const [shippingPending, setShippingPending] = useState(false)
  const shippingInFlight = useRef(false)
  const closeShipping = () => { if (!shippingInFlight.current) setShippingOrder(null) }
  const [shippingForm, setShippingForm] = useState({ tracking_number: '', carrier: '' })
  const [allOrders, setAllOrders] = useState([])

  useEffect(() => {
    loadData()
  }, [statusFilter])

  useEffect(() => {
    const onKey = (e) => { if (e.key !== 'Escape') return; if (shippingOrder) closeShipping(); else if (editingOrder) closeEdit(); else if (showDetailModal) setShowDetailModal(false); else if (showModal) setShowModal(false) }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [shippingOrder, editingOrder, showDetailModal, showModal])

  const loadData = async (committedShipment = null) => {
    try {
      const [ords, prods, all] = await Promise.all([orders.list(statusFilter || null), products.list(), statusFilter ? orders.list(null) : null])
      const preserveShipment = rows => rows.map(row => row.id === committedShipment?.id
        ? { ...row, status: committedShipment.status, tracking_number: committedShipment.tracking_number, shipped_date: committedShipment.shipped_date }
        : row)
      setOrderList(preserveShipment(ords).filter(row => !statusFilter || row.status === statusFilter)); setProductList(prods); setAllOrders(preserveShipment(statusFilter ? all : ords))
      return true
    } catch (err: any) { console.error('Failed to load:', err) }
    finally { setLoading(false) }
    return false
  }

  const openCreateModal = () => { setFormData(EMPTY_ORDER_FORM); setItems([]); setShowModal(true) }

  const openDetailModal = async (order) => {
    try {
      const full = await orders.get(order.id)
      setSelectedOrder(full)
      setShowDetailModal(true)
    } catch (err: any) {
      console.error('Failed to load order:', err)
    }
  }

  const handleSubmit = async (e) => {
    e.preventDefault()
    try {
      const data = {
        order_number: formData.order_number || null,
        platform: formData.platform,
        customer_name: formData.customer_name || null,
        customer_email: formData.customer_email || null,
        revenue: optionalAmount(formData.revenue),
        platform_fees: optionalAmount(formData.platform_fees),
        payment_fees: optionalAmount(formData.payment_fees),
        shipping_charged: optionalAmount(formData.shipping_charged),
        shipping_cost: optionalAmount(formData.shipping_cost),
        notes: formData.notes || null,
        items: items.filter(i => i.product_id).map(i => ({
          product_id: parseInt(i.product_id),
          quantity: parseInt(i.quantity) || 1,
          unit_price: optionalAmount(i.unit_price)
        }))
      }
      await orders.create(data)
      setShowModal(false)
      toast.success('Order created')
      loadData()
    } catch (err: any) {
      console.error('Failed to create order:', err)
      toast.error('Failed to create order')
    }
  }

  const openEditModal = async (order) => {
    const request = ++editRequest.current
    try {
      // The list is a summary. Load all editable fields before permitting a save.
      const full = await orders.get(order.id)
      if (request !== editRequest.current) return
      setEditFormData({
        customer_name: full.customer_name ?? '',
        customer_email: full.customer_email ?? '',
        platform: full.platform ?? 'etsy',
        order_number: full.order_number ?? '',
        notes: full.notes ?? '',
        tracking_number: full.tracking_number ?? '',
        revenue: full.revenue ?? '',
        platform_fees: full.platform_fees ?? '',
        payment_fees: full.payment_fees ?? '',
        shipping_charged: full.shipping_charged ?? '',
      })
      setEditItems((full.items || []).map(i => ({
        id: i.id,
        product_id: String(i.product_id),
        quantity: i.quantity,
        unit_price: i.unit_price ?? ''
      })))
      setEditingOrder(full)
    } catch {
      if (request !== editRequest.current) return
      toast.error('Could not load order for editing')
    }
  }

  const handleEditSubmit = async (e) => {
    e.preventDefault()
    try {
      await orders.update(editingOrder.id, {
        customer_name: editFormData.customer_name || null,
        customer_email: editFormData.customer_email || null,
        platform: editFormData.platform,
        order_number: editFormData.order_number || null,
        notes: editFormData.notes || null,
        tracking_number: editFormData.tracking_number || null,
        revenue: optionalAmount(editFormData.revenue),
        platform_fees: optionalAmount(editFormData.platform_fees),
        payment_fees: optionalAmount(editFormData.payment_fees),
        shipping_charged: optionalAmount(editFormData.shipping_charged),
      })
      // Sync line items
      const full = await orders.get(editingOrder.id)
      const existingIds = new Set((full.items || []).map(i => i.id))
      for (const item of editItems) {
        if (item.id && existingIds.has(item.id)) {
          await orders.updateItem(editingOrder.id, item.id, {
            quantity: parseInt(item.quantity) || 1,
            unit_price: optionalAmount(item.unit_price)
          })
        } else if (!item.id && item.product_id) {
          await orders.addItem(editingOrder.id, {
            product_id: parseInt(item.product_id),
            quantity: parseInt(item.quantity) || 1,
            unit_price: optionalAmount(item.unit_price)
          })
        }
      }
      // Remove deleted items
      const editIds = new Set(editItems.filter(i => i.id).map(i => i.id))
      for (const item of full.items || []) {
        if (!editIds.has(item.id)) {
          await orders.removeItem(editingOrder.id, item.id)
        }
      }
      setEditingOrder(null)
      toast.success('Order updated')
      loadData()
    } catch (err: any) {
      console.error('Failed to update order:', err)
      toast.error('Failed to update order')
    }
  }

  const handleDelete = (id) => setConfirmAction({ title: 'Delete Order', message: 'Delete this order? This cannot be undone.', onConfirm: async () => { try { await orders.delete(id); toast.success('Order deleted'); loadData() } catch { toast.error('Failed to delete order') } setConfirmAction(null) } })

  const handleSchedule = async (id) => { try { const r = await orders.schedule(id); toast.success(`Created ${r.jobs_created} jobs for this order`); loadData(); if (selectedOrder?.id === id) { const full = await orders.get(id); setSelectedOrder(full) } } catch { toast.error('Failed to schedule order') } }

  const handleConfirmProduction = async (itemId: number, quantity: number) => {
    const orderId = selectedOrder.id
    try {
      const updated = await orders.confirmProduction(orderId, itemId, quantity)
      setSelectedOrder(current => current?.id === orderId ? updated : current)
      toast.success('Production quantity confirmed')
      await loadData()
    } catch (error: any) {
      toast.error(error?.message || 'Failed to confirm production')
      throw error
    }
  }

  const handleShip = (order) => { if (shippingInFlight.current) return; setShippingForm({ tracking_number: order.tracking_number || '', carrier: '' }); setShippingOrder(order) }

  const submitShipping = async () => {
    if (!shippingOrder || shippingInFlight.current) return
    shippingInFlight.current = true
    setShippingPending(true)
    const orderId = shippingOrder.id
    let shipped = false
    try {
      const committed = await orders.ship(orderId, { tracking_number: shippingForm.tracking_number || null })
      shipped = true
      setOrderList(current => current.map(row => row.id === orderId ? { ...row, ...committed } : row).filter(row => !statusFilter || row.status === statusFilter))
      setAllOrders(current => current.map(row => row.id === orderId ? { ...row, ...committed } : row))
      setSelectedOrder(current => current?.id === orderId ? { ...current, ...committed } : current)
      toast.success('Order marked as shipped')
      setShippingOrder(current => current?.id === orderId ? null : current)
      if (!await loadData(committed)) throw new Error('Shipment refresh failed')
      if (selectedOrder?.id === orderId) {
        const full = await orders.get(orderId)
        setSelectedOrder(current => current?.id === orderId ? { ...full, status: committed.status, tracking_number: committed.tracking_number, shipped_date: committed.shipped_date } : current)
      }
    } catch {
      toast.error(shipped ? 'Order shipped, but details could not be refreshed' : 'Failed to mark as shipped')
    } finally {
      shippingInFlight.current = false
      setShippingPending(false)
    }
  }

  const handleCancel = (order) => setConfirmAction({ title: 'Cancel Order', message: `Cancel order ${order.order_number || '#' + order.id}?`, onConfirm: async () => { try { await orders.update(order.id, { status: 'cancelled' }); toast.success('Order cancelled'); loadData(); if (selectedOrder?.id === order.id) { const full = await orders.get(order.id); setSelectedOrder(full) } } catch { toast.error('Failed to cancel order') } setConfirmAction(null) } })

  const handleDownloadInvoice = async (order) => { try { await orderInvoice(order.id, order.order_number) } catch { toast.error('Failed to download invoice') } }

  const filteredOrders = searchQuery.trim()
    ? orderList.filter(o => {
        const q = searchQuery.toLowerCase()
        return (o.order_number || '').toLowerCase().includes(q) ||
               (o.customer_name || '').toLowerCase().includes(q)
      })
    : orderList

  if (loading) return <div className="flex items-center justify-center py-12 text-[var(--brand-muted)] gap-2"><RefreshCw size={16} className="animate-spin" />Loading...</div>

  return (
    <div className="p-4 md:p-6">
      <PageHeader icon={ShoppingCart} title="Orders">
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          className="rounded-md px-3 py-2 bg-[var(--brand-content-bg)] border border-[var(--brand-card-border)] text-[var(--brand-text)] text-sm"
        >
          <option value="">All Statuses</option>
          <option value="pending">Pending</option>
          <option value="in_progress">In Progress</option>
          <option value="partial">Partial</option>
          <option value="fulfilled">Fulfilled</option>
          <option value="shipped">Shipped</option>
          <option value="cancelled">Cancelled</option>
        </select>
        {canDo('orders.create') && (
          <Button variant="primary" icon={Plus} onClick={openCreateModal}>
            New Order
          </Button>
        )}
      </PageHeader>

      {/* Search */}
      <div className="mb-4">
        <SearchInput
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          placeholder="Search by order number or customer..."
        />
      </div>

      <OrderStatsBar orders={allOrders} />

      {filteredOrders.length === 0 ? (
        <EmptyState
          icon={ShoppingCart}
          title="No orders yet"
          description="Create your first order to start tracking."
        >
          {canDo('orders.create') && (
            <Button variant="primary" icon={Plus} onClick={openCreateModal}>
              New Order
            </Button>
          )}
        </EmptyState>
      ) : (
        <OrderTable
          orders={filteredOrders}
          STATUS_CLASSES={STATUS_CLASSES}
          onDetail={openDetailModal}
          onEdit={openEditModal}
          onSchedule={handleSchedule}
          onShip={handleShip}
          onCancel={handleCancel}
          onDelete={handleDelete}
        />
      )}

      <CreateOrderModal
        isOpen={showModal}
        onClose={() => setShowModal(false)}
        onSubmit={handleSubmit}
        formData={formData}
        setFormData={setFormData}
        items={items}
        setItems={setItems}
        productList={productList}
      />

      <OrderDetailModal
        isOpen={showDetailModal}
        order={selectedOrder}
        onClose={() => { setShowDetailModal(false); setSelectedOrder(null) }}
        onSchedule={handleSchedule}
        onShip={handleShip}
        onCancel={handleCancel}
        onDownloadInvoice={handleDownloadInvoice}
        onConfirmProduction={handleConfirmProduction}
        STATUS_CLASSES={STATUS_CLASSES}
        canDo={canDo}
      />

      <ShippingModal
        isOpen={!!shippingOrder}
        order={shippingOrder}
        shippingForm={shippingForm}
        setShippingForm={setShippingForm}
        onClose={closeShipping}
        isSubmitting={shippingPending}
        onSubmit={submitShipping}
      />

      <EditOrderModal
        isOpen={!!editingOrder}
        order={editingOrder}
        onClose={closeEdit}
        onSubmit={handleEditSubmit}
        editFormData={editFormData}
        setEditFormData={setEditFormData}
        editItems={editItems}
        setEditItems={setEditItems}
        productList={productList}
      />

      <ConfirmModal
        open={!!confirmAction}
        title={confirmAction?.title || ''}
        message={confirmAction?.message || ''}
        confirmText={confirmAction?.title === 'Delete Order' ? 'Delete' : 'Confirm'}
        onConfirm={() => confirmAction?.onConfirm()}
        onCancel={() => setConfirmAction(null)}
      />
    </div>
  )
}
