import { useState, useEffect } from 'react'
import { orders, products, orderInvoice } from '../../api'
import { ShoppingCart, Plus, RefreshCw, Search } from 'lucide-react'
import { canDo } from '../../permissions'
import toast from 'react-hot-toast'
import ConfirmModal from '../../components/shared/ConfirmModal'
import { CreateOrderModal, OrderDetailModal, ShippingModal, EditOrderModal } from '../../components/orders/OrderModals'
import OrderTable from '../../components/orders/OrderTable'

const STATUS_CLASSES = {
  pending: 'bg-status-pending/20 text-status-pending',
  in_progress: 'bg-status-scheduled/20 text-status-scheduled',
  partial: 'bg-purple-500/20 text-purple-400',
  fulfilled: 'bg-status-printing/20 text-status-printing',
  shipped: 'bg-status-completed/20 text-status-completed',
  cancelled: 'bg-status-failed/20 text-status-failed',
}

const EMPTY_ORDER_FORM = { order_number: '', platform: 'etsy', customer_name: '', customer_email: '', revenue: '', platform_fees: '', payment_fees: '', shipping_charged: '', shipping_cost: '', notes: '' }

function OrderStats({ orders }) {
  if (!orders.length) return null
  const stats = [
    { label: 'Pending', count: orders.filter(o => o.status === 'pending').length, color: 'text-yellow-400', bg: 'bg-yellow-400/10' },
    { label: 'In Progress', count: orders.filter(o => o.status === 'in_progress').length, color: 'text-blue-400', bg: 'bg-blue-400/10' },
    { label: 'Fulfilled', count: orders.filter(o => o.status === 'fulfilled').length, color: 'text-green-400', bg: 'bg-green-400/10' },
    { label: 'Shipped', count: orders.filter(o => o.status === 'shipped').length, color: 'text-purple-400', bg: 'bg-purple-400/10' },
    { label: 'Revenue', count: '$' + orders.reduce((s, o) => s + (o.revenue || 0), 0).toFixed(0), color: 'text-emerald-400', bg: 'bg-emerald-400/10' },
  ]
  return (
    <div className="grid grid-cols-2 md:grid-cols-5 gap-2 md:gap-3 mb-6">
      {stats.map(({ label, count, color, bg }) => (
        <div key={label} className={`${bg} rounded-lg p-3 text-center border border-farm-800`}>
          <div className={`text-lg md:text-xl font-bold tabular-nums ${color}`}>{count}</div>
          <div className="text-xs text-farm-500 uppercase tracking-wide">{label}</div>
        </div>
      ))}
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
  const [editFormData, setEditFormData] = useState({})
  const [editItems, setEditItems] = useState([])
  const [searchQuery, setSearchQuery] = useState('')
  const [confirmAction, setConfirmAction] = useState(null)
  const [shippingOrder, setShippingOrder] = useState(null)
  const [shippingForm, setShippingForm] = useState({ tracking_number: '', carrier: '' })
  const [allOrders, setAllOrders] = useState([])

  useEffect(() => {
    loadData()
  }, [statusFilter])

  useEffect(() => {
    const onKey = (e) => { if (e.key !== 'Escape') return; if (shippingOrder) setShippingOrder(null); else if (editingOrder) setEditingOrder(null); else if (showDetailModal) setShowDetailModal(false); else if (showModal) setShowModal(false) }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [shippingOrder, editingOrder, showDetailModal, showModal])

  const loadData = async () => {
    try {
      const [ords, prods, all] = await Promise.all([orders.list(statusFilter || null), products.list(), statusFilter ? orders.list(null) : null])
      setOrderList(ords); setProductList(prods); setAllOrders(statusFilter ? all : ords)
    } catch (err) { console.error('Failed to load:', err) }
    finally { setLoading(false) }
  }

  const openCreateModal = () => { setFormData(EMPTY_ORDER_FORM); setItems([]); setShowModal(true) }

  const openDetailModal = async (order) => {
    try {
      const full = await orders.get(order.id)
      setSelectedOrder(full)
      setShowDetailModal(true)
    } catch (err) {
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
        revenue: formData.revenue ? parseFloat(formData.revenue) : null,
        platform_fees: formData.platform_fees ? parseFloat(formData.platform_fees) : null,
        payment_fees: formData.payment_fees ? parseFloat(formData.payment_fees) : null,
        shipping_charged: formData.shipping_charged ? parseFloat(formData.shipping_charged) : null,
        shipping_cost: formData.shipping_cost ? parseFloat(formData.shipping_cost) : null,
        notes: formData.notes || null,
        items: items.filter(i => i.product_id).map(i => ({
          product_id: parseInt(i.product_id),
          quantity: parseInt(i.quantity) || 1,
          unit_price: i.unit_price ? parseFloat(i.unit_price) : null
        }))
      }
      await orders.create(data)
      setShowModal(false)
      toast.success('Order created')
      loadData()
    } catch (err) {
      console.error('Failed to create order:', err)
      toast.error('Failed to create order')
    }
  }

  const openEditModal = async (order) => {
    setEditFormData({
      customer_name: order.customer_name || '',
      customer_email: order.customer_email || '',
      platform: order.platform || 'etsy',
      order_number: order.order_number || '',
      notes: order.notes || '',
      tracking_number: order.tracking_number || '',
      revenue: order.revenue || '',
      platform_fees: order.platform_fees || '',
      payment_fees: order.payment_fees || '',
      shipping_charged: order.shipping_charged || '',
    })
    try {
      const full = await orders.get(order.id)
      setEditItems((full.items || []).map(i => ({
        id: i.id,
        product_id: String(i.product_id),
        quantity: i.quantity,
        unit_price: i.unit_price || ''
      })))
    } catch {
      setEditItems([])
    }
    setEditingOrder(order)
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
        revenue: editFormData.revenue ? parseFloat(editFormData.revenue) : null,
        platform_fees: editFormData.platform_fees ? parseFloat(editFormData.platform_fees) : null,
        payment_fees: editFormData.payment_fees ? parseFloat(editFormData.payment_fees) : null,
        shipping_charged: editFormData.shipping_charged ? parseFloat(editFormData.shipping_charged) : null,
      })
      // Sync line items
      const full = await orders.get(editingOrder.id)
      const existingIds = new Set((full.items || []).map(i => i.id))
      for (const item of editItems) {
        if (item.id && existingIds.has(item.id)) {
          await orders.updateItem(editingOrder.id, item.id, {
            quantity: parseInt(item.quantity) || 1,
            unit_price: item.unit_price ? parseFloat(item.unit_price) : null
          })
        } else if (!item.id && item.product_id) {
          await orders.addItem(editingOrder.id, {
            product_id: parseInt(item.product_id),
            quantity: parseInt(item.quantity) || 1,
            unit_price: item.unit_price ? parseFloat(item.unit_price) : null
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
    } catch (err) {
      console.error('Failed to update order:', err)
      toast.error('Failed to update order')
    }
  }

  const handleDelete = (id) => setConfirmAction({ title: 'Delete Order', message: 'Delete this order? This cannot be undone.', onConfirm: async () => { try { await orders.delete(id); toast.success('Order deleted'); loadData() } catch { toast.error('Failed to delete order') } setConfirmAction(null) } })

  const handleSchedule = async (id) => { try { const r = await orders.schedule(id); toast.success(`Created ${r.jobs_created} jobs for this order`); loadData(); if (selectedOrder?.id === id) { const full = await orders.get(id); setSelectedOrder(full) } } catch { toast.error('Failed to schedule order') } }

  const handleShip = (order) => { setShippingForm({ tracking_number: order.tracking_number || '', carrier: '' }); setShippingOrder(order) }

  const submitShipping = async () => { try { await orders.ship(shippingOrder.id, { tracking_number: shippingForm.tracking_number || null }); toast.success('Order marked as shipped'); loadData(); if (selectedOrder?.id === shippingOrder.id) { const full = await orders.get(shippingOrder.id); setSelectedOrder(full) } setShippingOrder(null) } catch { toast.error('Failed to mark as shipped') } }

  const handleCancel = (order) => setConfirmAction({ title: 'Cancel Order', message: `Cancel order ${order.order_number || '#' + order.id}?`, onConfirm: async () => { try { await orders.update(order.id, { status: 'cancelled' }); toast.success('Order cancelled'); loadData(); if (selectedOrder?.id === order.id) { const full = await orders.get(order.id); setSelectedOrder(full) } } catch { toast.error('Failed to cancel order') } setConfirmAction(null) } })

  const handleDownloadInvoice = async (order) => { try { await orderInvoice(order.id, order.order_number) } catch { toast.error('Failed to download invoice') } }

  const filteredOrders = searchQuery.trim()
    ? orderList.filter(o => {
        const q = searchQuery.toLowerCase()
        return (o.order_number || '').toLowerCase().includes(q) ||
               (o.customer_name || '').toLowerCase().includes(q)
      })
    : orderList

  if (loading) return <div className="flex items-center justify-center py-12 text-farm-500 gap-2"><RefreshCw size={16} className="animate-spin" />Loading...</div>

  return (
    <div className="p-4 md:p-6">
      <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4 mb-6">
        <div className="flex items-center gap-3">
          <ShoppingCart className="text-print-400" size={24} />
          <h1 className="text-xl md:text-2xl font-display font-bold">Orders</h1>
        </div>
        <div className="flex flex-col sm:flex-row gap-2 sm:gap-4 w-full sm:w-auto">
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="rounded-lg px-3 py-2 bg-farm-950 border border-farm-700 text-farm-100 text-sm"
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
            <button
              onClick={openCreateModal}
              className="px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg transition-colors text-sm flex items-center justify-center gap-2"
            >
              <Plus size={16} /> New Order
            </button>
          )}
        </div>
      </div>

      {/* Search */}
      <div className="mb-4">
        <div className="relative">
          <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-farm-500" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search by order number or customer..."
            className="w-full bg-farm-950 border border-farm-700 rounded-lg pl-9 pr-3 py-2 text-sm text-farm-100 placeholder-farm-500"
          />
        </div>
      </div>

      <OrderStats orders={allOrders} />

      {filteredOrders.length === 0 ? (
        <div className="text-center py-12 text-farm-500">
          <ShoppingCart className="w-12 h-12 mx-auto mb-4 opacity-50" />
          <p>No orders yet. Create your first order to start tracking.</p>
        </div>
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
        onClose={() => setShowDetailModal(false)}
        onSchedule={handleSchedule}
        onShip={handleShip}
        onCancel={handleCancel}
        onDownloadInvoice={handleDownloadInvoice}
        STATUS_CLASSES={STATUS_CLASSES}
        canDo={canDo}
      />

      <ShippingModal
        isOpen={!!shippingOrder}
        order={shippingOrder}
        shippingForm={shippingForm}
        setShippingForm={setShippingForm}
        onClose={() => setShippingOrder(null)}
        onSubmit={submitShipping}
      />

      <EditOrderModal
        isOpen={!!editingOrder}
        order={editingOrder}
        onClose={() => setEditingOrder(null)}
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
