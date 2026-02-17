import { useState, useEffect } from 'react'
import { orders, products, orderInvoice } from '../api'
import { ShoppingCart, Plus, Trash2, Eye, X, Save, Truck, Play, FileText, Pencil, RefreshCw, Search, Ban } from 'lucide-react'
import { canDo } from '../permissions'
import toast from 'react-hot-toast'
import ConfirmModal from '../components/ConfirmModal'

const STATUS_CLASSES = {
  pending: 'bg-status-pending/20 text-status-pending',
  in_progress: 'bg-status-scheduled/20 text-status-scheduled',
  partial: 'bg-purple-500/20 text-purple-400',
  fulfilled: 'bg-status-printing/20 text-status-printing',
  shipped: 'bg-status-completed/20 text-status-completed',
  cancelled: 'bg-status-failed/20 text-status-failed',
}

const PLATFORMS = ['etsy', 'amazon', 'ebay', 'direct', 'wholesale', 'other']

export default function Orders() {
  const [orderList, setOrderList] = useState([])
  const [productList, setProductList] = useState([])
  const [loading, setLoading] = useState(true)
  const [statusFilter, setStatusFilter] = useState('')
  const [showModal, setShowModal] = useState(false)
  const [showDetailModal, setShowDetailModal] = useState(false)
  const [selectedOrder, setSelectedOrder] = useState(null)
  const [formData, setFormData] = useState({
    order_number: '',
    platform: 'etsy',
    customer_name: '',
    customer_email: '',
    revenue: '',
    platform_fees: '',
    payment_fees: '',
    shipping_charged: '',
    shipping_cost: '',
    notes: ''
  })
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
    const handleKey = (e) => {
      if (e.key === 'Escape') {
        if (shippingOrder) setShippingOrder(null)
        else if (editingOrder) setEditingOrder(null)
        else if (showDetailModal) setShowDetailModal(false)
        else if (showModal) setShowModal(false)
      }
    }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
  }, [shippingOrder, editingOrder, showDetailModal, showModal])

  const loadData = async () => {
    try {
      const [ords, prods, all] = await Promise.all([
        orders.list(statusFilter || null),
        products.list(),
        statusFilter ? orders.list(null) : null
      ])
      setOrderList(ords)
      setProductList(prods)
      setAllOrders(statusFilter ? all : ords)
    } catch (err) {
      console.error('Failed to load:', err)
    } finally {
      setLoading(false)
    }
  }

  const openCreateModal = () => {
    setFormData({
      order_number: '',
      platform: 'etsy',
      customer_name: '',
      customer_email: '',
      revenue: '',
      platform_fees: '',
      payment_fees: '',
      shipping_charged: '',
      shipping_cost: '',
      notes: ''
    })
    setItems([])
    setShowModal(true)
  }

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

  const handleDelete = (id) => {
    setConfirmAction({
      title: 'Delete Order',
      message: 'Delete this order? This cannot be undone.',
      onConfirm: async () => {
        try {
          await orders.delete(id)
          toast.success('Order deleted')
          loadData()
        } catch (err) {
          toast.error('Failed to delete order')
        }
        setConfirmAction(null)
      }
    })
  }

  const handleSchedule = async (id) => {
    try {
      const result = await orders.schedule(id)
      toast.success(`Created ${result.jobs_created} jobs for this order`)
      loadData()
      if (selectedOrder?.id === id) {
        const full = await orders.get(id)
        setSelectedOrder(full)
      }
    } catch (err) {
      console.error('Failed to schedule:', err)
      toast.error('Failed to schedule order')
    }
  }

  const handleShip = (order) => {
    setShippingForm({ tracking_number: order.tracking_number || '', carrier: '' })
    setShippingOrder(order)
  }

  const submitShipping = async () => {
    try {
      await orders.ship(shippingOrder.id, {
        tracking_number: shippingForm.tracking_number || null
      })
      toast.success('Order marked as shipped')
      loadData()
      if (selectedOrder?.id === shippingOrder.id) {
        const full = await orders.get(shippingOrder.id)
        setSelectedOrder(full)
      }
      setShippingOrder(null)
    } catch (err) {
      console.error('Failed to ship:', err)
      toast.error('Failed to mark as shipped')
    }
  }

  const handleCancel = (order) => {
    setConfirmAction({
      title: 'Cancel Order',
      message: `Cancel order ${order.order_number || '#' + order.id}? This will set the status to cancelled.`,
      onConfirm: async () => {
        try {
          await orders.update(order.id, { status: 'cancelled' })
          toast.success('Order cancelled')
          loadData()
          if (selectedOrder?.id === order.id) {
            const full = await orders.get(order.id)
            setSelectedOrder(full)
          }
        } catch (err) {
          toast.error('Failed to cancel order')
        }
        setConfirmAction(null)
      }
    })
  }

  const handleDownloadInvoice = async (order) => {
    try {
      await orderInvoice(order.id, order.order_number)
    } catch (err) {
      console.error('Invoice download failed:', err)
      toast.error('Failed to download invoice')
    }
  }

  const addItem = () => {
    setItems([...items, { product_id: '', quantity: 1, unit_price: '' }])
  }

  const updateItem = (index, field, value) => {
    const updated = [...items]
    updated[index][field] = value
    if (field === 'product_id' && value) {
      const product = productList.find(p => p.id === parseInt(value))
      if (product?.price) {
        updated[index].unit_price = product.price
      }
    }
    setItems(updated)
  }

  const removeItem = (index) => {
    setItems(items.filter((_, i) => i !== index))
  }

  const calcItemsTotal = () => {
    return items.reduce((sum, item) => {
      const qty = parseInt(item.quantity) || 0
      const price = parseFloat(item.unit_price) || 0
      return sum + (qty * price)
    }, 0)
  }

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

      {/* Order Summary */}
      {(allOrders.length > 0 || orderList.length > 0) && (
        <div className="grid grid-cols-2 md:grid-cols-5 gap-2 md:gap-3 mb-6">
          {[
            { label: 'Pending', count: allOrders.filter(o => o.status === 'pending').length, color: 'text-yellow-400', bg: 'bg-yellow-400/10' },
            { label: 'In Progress', count: allOrders.filter(o => o.status === 'in_progress').length, color: 'text-blue-400', bg: 'bg-blue-400/10' },
            { label: 'Fulfilled', count: allOrders.filter(o => o.status === 'fulfilled').length, color: 'text-green-400', bg: 'bg-green-400/10' },
            { label: 'Shipped', count: allOrders.filter(o => o.status === 'shipped').length, color: 'text-purple-400', bg: 'bg-purple-400/10' },
            { label: 'Revenue', count: '$' + allOrders.reduce((s, o) => s + (o.revenue || 0), 0).toFixed(0), color: 'text-emerald-400', bg: 'bg-emerald-400/10' },
          ].map(({ label, count, color, bg }) => (
            <div key={label} className={`${bg} rounded-lg p-3 text-center border border-farm-800`}>
              <div className={`text-lg md:text-xl font-bold tabular-nums ${color}`}>{count}</div>
              <div className="text-xs text-farm-500 uppercase tracking-wide">{label}</div>
            </div>
          ))}
        </div>
      )}

      {filteredOrders.length === 0 ? (
        <div className="text-center py-12 text-farm-500">
          <ShoppingCart className="w-12 h-12 mx-auto mb-4 opacity-50" />
          <p>No orders yet. Create your first order to start tracking.</p>
        </div>
      ) : (
        <div className="bg-farm-900 rounded-lg border border-farm-800 overflow-hidden">
          {/* Mobile card view */}
          <div className="block md:hidden divide-y divide-farm-800">
            {filteredOrders.map(order => (
              <div key={order.id} className="p-4">
                <div className="flex justify-between items-start mb-2">
                  <button
                    onClick={() => openDetailModal(order)}
                    className="font-medium text-print-400 hover:text-print-300"
                  >
                    {order.order_number || `#${order.id}`}
                  </button>
                  <span className={`px-2 py-1 rounded-lg text-xs ${STATUS_CLASSES[order.status] || 'bg-farm-700 text-farm-300'}`}>
                    {order.status?.replace('_', ' ')}
                  </span>
                </div>
                <div className="text-sm space-y-1 text-farm-400 mb-3">
                  <div>Platform: {order.platform || '-'}</div>
                  <div>Customer: {order.customer_name || '-'}</div>
                  <div>Items: {order.item_count || 0}</div>
                  <div>Revenue: {order.revenue ? `$${order.revenue.toFixed(2)}` : '-'}</div>
                  {order.created_at && <div>Date: {new Date(order.created_at).toLocaleDateString()}</div>}
                </div>
                <div className="flex gap-1">
                  <button
                    onClick={() => openDetailModal(order)}
                    className="p-1 md:p-1.5 text-print-400 hover:bg-print-900/50 rounded-lg transition-colors"
                    title="View Details"
                  >
                    <Eye size={14} />
                  </button>
                  {canDo('orders.edit') && (
                    <button
                      onClick={() => openEditModal(order)}
                      className="p-1 md:p-1.5 text-farm-400 hover:bg-farm-800 rounded-lg transition-colors"
                      title="Edit Order"
                    >
                      <Pencil size={14} />
                    </button>
                  )}
                  {canDo('orders.edit') && order.status === 'pending' && (
                    <button
                      onClick={() => handleSchedule(order.id)}
                      className="p-1 md:p-1.5 text-print-400 hover:bg-print-900/50 rounded-lg transition-colors"
                      title="Schedule Jobs"
                    >
                      <Play size={14} />
                    </button>
                  )}
                  {canDo('orders.ship') && order.status === 'fulfilled' && (
                    <button
                      onClick={() => handleShip(order)}
                      className="p-1 md:p-1.5 text-print-400 hover:bg-print-900/50 rounded-lg transition-colors"
                      title="Mark Shipped"
                    >
                      <Truck size={14} />
                    </button>
                  )}
                  {canDo('orders.edit') && (order.status === 'pending' || order.status === 'in_progress') && (
                    <button
                      onClick={() => handleCancel(order)}
                      className="p-1 md:p-1.5 text-farm-500 hover:text-red-400 hover:bg-red-900/50 rounded-lg transition-colors"
                      title="Cancel Order"
                    >
                      <Ban size={14} />
                    </button>
                  )}
                  {canDo('orders.delete') && (
                    <button
                      onClick={() => handleDelete(order.id)}
                      className="p-1 md:p-1.5 text-farm-500 hover:text-red-400 hover:bg-red-900/50 rounded-lg transition-colors"
                      title="Delete"
                    >
                      <Trash2 size={14} />
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>

          {/* Desktop table view */}
          <table className="w-full hidden md:table">
            <thead className="bg-farm-950">
              <tr>
                <th className="px-4 py-3 text-left text-sm font-medium text-farm-400">Order #</th>
                <th className="px-4 py-3 text-left text-sm font-medium text-farm-400">Date</th>
                <th className="px-4 py-3 text-left text-sm font-medium text-farm-400">Platform</th>
                <th className="px-4 py-3 text-left text-sm font-medium text-farm-400">Customer</th>
                <th className="px-4 py-3 text-left text-sm font-medium text-farm-400">Status</th>
                <th className="px-4 py-3 text-left text-sm font-medium text-farm-400">Items</th>
                <th className="px-4 py-3 text-left text-sm font-medium text-farm-400">Revenue</th>
                <th className="px-4 py-3 text-right text-sm font-medium text-farm-400">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-farm-800">
              {filteredOrders.map(order => (
                <tr key={order.id} className="hover:bg-farm-800/50 transition-colors">
                  <td className="px-4 py-3">
                    <button
                      onClick={() => openDetailModal(order)}
                      className="font-medium text-print-400 hover:text-print-300"
                    >
                      {order.order_number || `#${order.id}`}
                    </button>
                  </td>
                  <td className="px-4 py-3 text-farm-400 text-sm">
                    {order.created_at ? new Date(order.created_at).toLocaleDateString() : '-'}
                  </td>
                  <td className="px-4 py-3 capitalize text-farm-300">{order.platform || '-'}</td>
                  <td className="px-4 py-3 text-farm-300">{order.customer_name || '-'}</td>
                  <td className="px-4 py-3">
                    <span className={`px-2 py-1 rounded-lg text-sm ${STATUS_CLASSES[order.status] || 'bg-farm-700 text-farm-300'}`}>
                      {order.status?.replace('_', ' ')}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-farm-300">{order.item_count || 0}</td>
                  <td className="px-4 py-3 text-farm-200">{order.revenue ? `$${order.revenue.toFixed(2)}` : '-'}</td>
                  <td className="px-4 py-3 text-right">
                    <button
                      onClick={() => openDetailModal(order)}
                      className="p-1 md:p-1.5 text-print-400 hover:bg-print-900/50 rounded-lg transition-colors"
                      title="View Details"
                    >
                      <Eye size={14} />
                    </button>
                    {canDo('orders.edit') && (
                      <button
                        onClick={() => openEditModal(order)}
                        className="p-1 md:p-1.5 text-farm-400 hover:bg-farm-800 rounded-lg transition-colors"
                        title="Edit Order"
                      >
                        <Pencil size={14} />
                      </button>
                    )}
                    {canDo('orders.edit') && order.status === 'pending' && (
                      <button
                        onClick={() => handleSchedule(order.id)}
                        className="p-1 md:p-1.5 text-print-400 hover:bg-print-900/50 rounded-lg transition-colors"
                        title="Schedule Jobs"
                      >
                        <Play size={14} />
                      </button>
                    )}
                    {canDo('orders.ship') && order.status === 'fulfilled' && (
                      <button
                        onClick={() => handleShip(order)}
                        className="p-1 md:p-1.5 text-print-400 hover:bg-print-900/50 rounded-lg transition-colors"
                        title="Mark Shipped"
                      >
                        <Truck size={14} />
                      </button>
                    )}
                    {canDo('orders.edit') && (order.status === 'pending' || order.status === 'in_progress') && (
                      <button
                        onClick={() => handleCancel(order)}
                        className="p-1 md:p-1.5 text-farm-500 hover:text-red-400 hover:bg-red-900/50 rounded-lg transition-colors"
                        title="Cancel Order"
                      >
                        <Ban size={14} />
                      </button>
                    )}
                    {canDo('orders.delete') && (
                      <button
                        onClick={() => handleDelete(order.id)}
                        className="p-1 md:p-1.5 text-farm-500 hover:text-red-400 hover:bg-red-900/50 rounded-lg transition-colors"
                        title="Delete"
                      >
                        <Trash2 size={14} />
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Create Order Modal */}
      {showModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4" onClick={() => setShowModal(false)} onKeyDown={(e) => e.key === 'Escape' && setShowModal(false)}>
          <div className="bg-farm-900 rounded-lg border border-farm-800 w-full max-w-2xl max-h-[90vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
            <div className="p-4 border-b border-farm-800 flex justify-between items-center">
              <h2 className="text-lg font-semibold text-farm-100">New Order</h2>
              <button onClick={() => setShowModal(false)} className="p-1 text-farm-500 hover:text-farm-300 hover:bg-farm-800 rounded-lg transition-colors">
                <X size={18} />
              </button>
            </div>
            <form onSubmit={handleSubmit} className="p-4 space-y-4">
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium mb-1 text-farm-200">Order Number</label>
                  <input
                    type="text"
                    value={formData.order_number}
                    onChange={(e) => setFormData({ ...formData, order_number: e.target.value })}
                    className="w-full rounded-lg px-3 py-2 bg-farm-950 border border-farm-700 text-farm-100 focus:border-print-500 focus:outline-none text-sm"
                    placeholder="ETSY-12345"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium mb-1 text-farm-200">Platform</label>
                  <select
                    value={formData.platform}
                    onChange={(e) => setFormData({ ...formData, platform: e.target.value })}
                    className="w-full rounded-lg px-3 py-2 bg-farm-950 border border-farm-700 text-farm-100 text-sm"
                  >
                    {PLATFORMS.map(p => (
                      <option key={p} value={p}>{p.charAt(0).toUpperCase() + p.slice(1)}</option>
                    ))}
                  </select>
                </div>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium mb-1 text-farm-200">Customer Name</label>
                  <input
                    type="text"
                    value={formData.customer_name}
                    onChange={(e) => setFormData({ ...formData, customer_name: e.target.value })}
                    className="w-full rounded-lg px-3 py-2 bg-farm-950 border border-farm-700 text-farm-100 focus:border-print-500 focus:outline-none text-sm"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium mb-1 text-farm-200">Customer Email</label>
                  <input
                    type="email"
                    value={formData.customer_email}
                    onChange={(e) => setFormData({ ...formData, customer_email: e.target.value })}
                    className="w-full rounded-lg px-3 py-2 bg-farm-950 border border-farm-700 text-farm-100 focus:border-print-500 focus:outline-none text-sm"
                  />
                </div>
              </div>

              {/* Line Items */}
              <div>
                <div className="flex justify-between items-center mb-2">
                  <label className="block text-sm font-medium text-farm-200">Line Items</label>
                  <button type="button" onClick={addItem} className="text-xs text-print-400 hover:text-print-300 flex items-center gap-1">
                    <Plus size={14} /> Add Item
                  </button>
                </div>
                {items.length === 0 ? (
                  <p className="text-sm text-farm-500">Add products to this order</p>
                ) : (
                  <div className="space-y-2">
                    {items.map((item, i) => (
                      <div key={i} className="flex flex-col sm:flex-row gap-2 items-stretch sm:items-center">
                        <select
                          value={item.product_id}
                          onChange={(e) => updateItem(i, 'product_id', e.target.value)}
                          className="flex-1 rounded-lg px-2 py-1.5 text-sm bg-farm-950 border border-farm-700 text-farm-100"
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
                            className="w-16 rounded-lg px-2 py-1.5 text-sm bg-farm-950 border border-farm-700 text-farm-100"
                            placeholder="Qty"
                          />
                          <input
                            type="number"
                            step="0.01"
                            value={item.unit_price}
                            onChange={(e) => updateItem(i, 'unit_price', e.target.value)}
                            className="w-24 rounded-lg px-2 py-1.5 text-sm bg-farm-950 border border-farm-700 text-farm-100"
                            placeholder="Price"
                          />
                          <button 
                            type="button" 
                            onClick={() => removeItem(i)} 
                            className="p-2 text-farm-500 hover:text-red-400 hover:bg-red-900/30 rounded-lg transition-colors"
                          >
                            <X size={14} />
                          </button>
                        </div>
                      </div>
                    ))}
                    <div className="text-right text-sm text-farm-400">
                      Subtotal: ${calcItemsTotal().toFixed(2)}
                    </div>
                  </div>
                )}
              </div>

              {/* Financials */}
              <div className="border-t border-farm-800 pt-4">
                <label className="block text-sm font-medium mb-2 text-farm-200">Financials</label>
                <div className="grid grid-cols-2 sm:grid-cols-3 gap-4">
                  <div>
                    <label className="block text-xs mb-1 text-farm-400">Revenue</label>
                    <input type="number" step="0.01" value={formData.revenue} onChange={(e) => setFormData({ ...formData, revenue: e.target.value })} className="w-full rounded-lg px-3 py-2 bg-farm-950 border border-farm-700 text-farm-100 text-sm" placeholder="0.00" />
                  </div>
                  <div>
                    <label className="block text-xs mb-1 text-farm-400">Platform Fees</label>
                    <input type="number" step="0.01" value={formData.platform_fees} onChange={(e) => setFormData({ ...formData, platform_fees: e.target.value })} className="w-full rounded-lg px-3 py-2 bg-farm-950 border border-farm-700 text-farm-100 text-sm" placeholder="0.00" />
                  </div>
                  <div>
                    <label className="block text-xs mb-1 text-farm-400">Payment Fees</label>
                    <input type="number" step="0.01" value={formData.payment_fees} onChange={(e) => setFormData({ ...formData, payment_fees: e.target.value })} className="w-full rounded-lg px-3 py-2 bg-farm-950 border border-farm-700 text-farm-100 text-sm" placeholder="0.00" />
                  </div>
                  <div>
                    <label className="block text-xs mb-1 text-farm-400">Shipping Charged</label>
                    <input type="number" step="0.01" value={formData.shipping_charged} onChange={(e) => setFormData({ ...formData, shipping_charged: e.target.value })} className="w-full rounded-lg px-3 py-2 bg-farm-950 border border-farm-700 text-farm-100 text-sm" placeholder="0.00" />
                  </div>
                  <div>
                    <label className="block text-xs mb-1 text-farm-400">Shipping Cost</label>
                    <input type="number" step="0.01" value={formData.shipping_cost} onChange={(e) => setFormData({ ...formData, shipping_cost: e.target.value })} className="w-full rounded-lg px-3 py-2 bg-farm-950 border border-farm-700 text-farm-100 text-sm" placeholder="0.00" />
                  </div>
                </div>
              </div>

              <div>
                <label className="block text-sm font-medium mb-1 text-farm-200">Notes</label>
                <textarea value={formData.notes} onChange={(e) => setFormData({ ...formData, notes: e.target.value })} className="w-full rounded-lg px-3 py-2 bg-farm-950 border border-farm-700 text-farm-100 text-sm" rows={2} />
              </div>

              <div className="flex justify-end gap-2 pt-4 border-t border-farm-800">
                <button type="button" onClick={() => setShowModal(false)} className="px-4 py-2 bg-farm-800 hover:bg-farm-700 rounded-lg transition-colors text-sm">
                  Cancel
                </button>
                <button type="submit" className="px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg transition-colors text-sm">
                  Create Order
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Order Detail Modal */}
      {showDetailModal && selectedOrder && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4" onClick={() => setShowDetailModal(false)}>
          <div className="bg-farm-900 rounded-lg border border-farm-800 w-full max-w-2xl max-h-[90vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
            <div className="p-4 border-b border-farm-800 flex justify-between items-center">
              <h2 className="text-lg font-semibold text-farm-100">
                Order {selectedOrder.order_number || `#${selectedOrder.id}`}
              </h2>
              <button onClick={() => setShowDetailModal(false)} className="p-1 text-farm-500 hover:text-farm-300 hover:bg-farm-800 rounded-lg transition-colors">
                <X size={18} />
              </button>
            </div>
            <div className="p-4 space-y-4">
              {/* Status & Actions */}
              <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-2">
                <span className={`px-3 py-1 rounded-lg text-sm ${STATUS_CLASSES[selectedOrder.status] || 'bg-farm-700 text-farm-300'}`}>
                  {selectedOrder.status?.replace('_', ' ')}
                </span>
                <div className="flex gap-2">
                  {canDo('orders.edit') && selectedOrder.status === 'pending' && (
                    <button
                      onClick={() => handleSchedule(selectedOrder.id)}
                      className="px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg transition-colors text-sm"
                    >
                      Schedule Jobs
                    </button>
                  )}
                  {canDo('orders.ship') && selectedOrder.status === 'fulfilled' && (
                    <button
                      onClick={() => handleShip(selectedOrder)}
                      className="px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg transition-colors text-sm"
                    >
                      Mark Shipped
                    </button>
                  )}
                  {canDo('orders.edit') && (selectedOrder.status === 'pending' || selectedOrder.status === 'in_progress') && (
                    <button
                      onClick={() => handleCancel(selectedOrder)}
                      className="px-4 py-2 bg-red-600 hover:bg-red-500 rounded-lg transition-colors text-sm flex items-center gap-2"
                    >
                      <Ban size={14} /> Cancel Order
                    </button>
                  )}
                  <button
                    onClick={() => handleDownloadInvoice(selectedOrder)}
                    className="px-4 py-2 bg-farm-800 hover:bg-farm-700 rounded-lg transition-colors text-sm flex items-center gap-2"
                    title="Download Invoice PDF"
                  >
                    <FileText size={14} /> Invoice
                  </button>
                </div>
              </div>

              {/* Customer Info */}
              <div className="grid grid-cols-2 gap-4 text-sm">
                <div><span className="text-farm-500">Platform:</span> <span className="text-farm-200">{selectedOrder.platform || '-'}</span></div>
                <div><span className="text-farm-500">Customer:</span> <span className="text-farm-200">{selectedOrder.customer_name || '-'}</span></div>
              </div>

              {/* Line Items */}
              <div>
                <h3 className="font-medium mb-2 text-farm-200">Items</h3>
                <div className="rounded-lg p-3 space-y-2 bg-farm-950">
                  {selectedOrder.items?.map(item => (
                    <div key={item.id} className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-1">
                      <div className="text-farm-200">
                        <span className="font-medium">{item.quantity}x</span> {item.product_name || `Product #${item.product_id}`}
                        {item.product_sku && <span className="text-sm ml-2 text-farm-500">({item.product_sku})</span>}
                      </div>
                      <div className="flex items-center gap-4 text-sm">
                        <span className="text-farm-300">${item.subtotal?.toFixed(2)}</span>
                        <span className={item.is_fulfilled ? 'text-status-printing' : 'text-status-pending'}>
                          {item.is_fulfilled ? '✓ Done' : `${item.fulfilled_quantity}/${item.quantity}`}
                        </span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              {/* Jobs Progress */}
              <div>
                <h3 className="font-medium mb-2 text-farm-200">Jobs</h3>
                <div className="text-sm text-farm-300">
                  {selectedOrder.jobs_complete} / {selectedOrder.jobs_total} complete
                  <div className="w-full rounded-lg h-2 mt-1 bg-farm-800">
                    <div
                      className="h-2 rounded-lg bg-print-500"
                      style={{ width: `${selectedOrder.jobs_total ? (selectedOrder.jobs_complete / selectedOrder.jobs_total) * 100 : 0}%` }}
                    />
                  </div>
                </div>
              </div>

              {/* P&L */}
              <div>
                <h3 className="font-medium mb-2 text-farm-200">Profit & Loss</h3>
                <div className="rounded-lg p-3 space-y-1 text-sm bg-farm-950">
                  <div className="flex justify-between">
                    <span className="text-farm-400">Revenue</span>
                    <span className="font-medium text-farm-200">${selectedOrder.revenue?.toFixed(2) || '0.00'}</span>
                  </div>
                  {selectedOrder.platform_fees > 0 && (
                    <div className="flex justify-between text-farm-400">
                      <span>Platform Fees</span>
                      <span>-${selectedOrder.platform_fees.toFixed(2)}</span>
                    </div>
                  )}
                  {selectedOrder.payment_fees > 0 && (
                    <div className="flex justify-between text-farm-400">
                      <span>Payment Fees</span>
                      <span>-${selectedOrder.payment_fees.toFixed(2)}</span>
                    </div>
                  )}
                  {selectedOrder.shipping_cost > 0 && (
                    <div className="flex justify-between text-farm-400">
                      <span>Shipping Cost</span>
                      <span>-${selectedOrder.shipping_cost.toFixed(2)}</span>
                    </div>
                  )}
                  {selectedOrder.actual_cost > 0 && (
                    <div className="flex justify-between text-farm-400">
                      <span>Production Cost</span>
                      <span>-${selectedOrder.actual_cost.toFixed(2)}</span>
                    </div>
                  )}
                  {selectedOrder.profit !== null && selectedOrder.profit !== undefined && (
                    <>
                      <div className="flex justify-between font-medium pt-1 mt-1 border-t border-farm-800">
                        <span className="text-farm-200">Profit</span>
                        <span className={selectedOrder.profit >= 0 ? 'text-status-printing' : 'text-status-failed'}>
                          ${selectedOrder.profit?.toFixed(2)}
                        </span>
                      </div>
                      {selectedOrder.margin_percent && (
                        <div className="flex justify-between text-farm-400">
                          <span>Margin</span>
                          <span>{selectedOrder.margin_percent}%</span>
                        </div>
                      )}
                    </>
                  )}
                </div>
              </div>

              {selectedOrder.tracking_number && (
                <div className="text-sm">
                  <span className="text-farm-500">Tracking:</span>{' '}
                  <span className="text-farm-200">{selectedOrder.tracking_number}</span>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Shipping Modal */}
      {shippingOrder && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4" onClick={() => setShippingOrder(null)}>
          <div className="bg-farm-900 rounded-lg border border-farm-800 w-full max-w-md" onClick={e => e.stopPropagation()}>
            <div className="p-4 border-b border-farm-800 flex justify-between items-center">
              <h2 className="text-lg font-semibold text-farm-100">Mark as Shipped</h2>
              <button onClick={() => setShippingOrder(null)} className="p-1 text-farm-500 hover:text-farm-300 hover:bg-farm-800 rounded-lg transition-colors">
                <X size={18} />
              </button>
            </div>
            <div className="p-4 space-y-4">
              <div>
                <label className="block text-sm font-medium mb-1 text-farm-200">Tracking Number</label>
                <input
                  type="text"
                  value={shippingForm.tracking_number}
                  onChange={(e) => setShippingForm(f => ({ ...f, tracking_number: e.target.value }))}
                  className="w-full rounded-lg px-3 py-2 bg-farm-950 border border-farm-700 text-farm-100 text-sm"
                  placeholder="Enter tracking number (optional)"
                />
              </div>
              <div>
                <label className="block text-sm font-medium mb-1 text-farm-200">Carrier</label>
                <select
                  value={shippingForm.carrier}
                  onChange={(e) => setShippingForm(f => ({ ...f, carrier: e.target.value }))}
                  className="w-full rounded-lg px-3 py-2 bg-farm-950 border border-farm-700 text-farm-100 text-sm"
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
                <button onClick={() => setShippingOrder(null)} className="px-4 py-2 bg-farm-800 hover:bg-farm-700 rounded-lg transition-colors text-sm">Cancel</button>
                <button onClick={submitShipping} className="px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg transition-colors text-sm flex items-center gap-2">
                  <Truck size={14} /> Mark Shipped
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Edit Order Modal */}
      {editingOrder && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4" onClick={() => setEditingOrder(null)}>
          <div className="bg-farm-900 rounded-lg border border-farm-800 w-full max-w-2xl max-h-[90vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
            <div className="p-4 border-b border-farm-800 flex justify-between items-center">
              <h2 className="text-lg font-semibold text-farm-100">Edit Order {editingOrder.order_number || `#${editingOrder.id}`}</h2>
              <button onClick={() => setEditingOrder(null)} className="p-1 text-farm-500 hover:text-farm-300 hover:bg-farm-800 rounded-lg transition-colors">
                <X size={18} />
              </button>
            </div>
            <form onSubmit={handleEditSubmit} className="p-4 space-y-4">
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium mb-1 text-farm-200">Order Number</label>
                  <input type="text" value={editFormData.order_number} onChange={(e) => setEditFormData(d => ({ ...d, order_number: e.target.value }))} className="w-full rounded-lg px-3 py-2 bg-farm-950 border border-farm-700 text-farm-100 text-sm" />
                </div>
                <div>
                  <label className="block text-sm font-medium mb-1 text-farm-200">Platform</label>
                  <select value={editFormData.platform} onChange={(e) => setEditFormData(d => ({ ...d, platform: e.target.value }))} className="w-full rounded-lg px-3 py-2 bg-farm-950 border border-farm-700 text-farm-100 text-sm">
                    {PLATFORMS.map(p => (
                      <option key={p} value={p}>{p.charAt(0).toUpperCase() + p.slice(1)}</option>
                    ))}
                  </select>
                </div>
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium mb-1 text-farm-200">Customer Name</label>
                  <input type="text" value={editFormData.customer_name} onChange={(e) => setEditFormData(d => ({ ...d, customer_name: e.target.value }))} className="w-full rounded-lg px-3 py-2 bg-farm-950 border border-farm-700 text-farm-100 text-sm" />
                </div>
                <div>
                  <label className="block text-sm font-medium mb-1 text-farm-200">Customer Email</label>
                  <input type="email" value={editFormData.customer_email} onChange={(e) => setEditFormData(d => ({ ...d, customer_email: e.target.value }))} className="w-full rounded-lg px-3 py-2 bg-farm-950 border border-farm-700 text-farm-100 text-sm" />
                </div>
              </div>
              <div>
                <label className="block text-sm font-medium mb-1 text-farm-200">Tracking Number</label>
                <input type="text" value={editFormData.tracking_number} onChange={(e) => setEditFormData(d => ({ ...d, tracking_number: e.target.value }))} className="w-full rounded-lg px-3 py-2 bg-farm-950 border border-farm-700 text-farm-100 text-sm" placeholder="Optional" />
              </div>
              <div className="border-t border-farm-800 pt-4">
                <label className="block text-sm font-medium mb-2 text-farm-200">Financials</label>
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
                  <div>
                    <label className="block text-xs mb-1 text-farm-400">Revenue</label>
                    <input type="number" step="0.01" value={editFormData.revenue} onChange={(e) => setEditFormData(d => ({ ...d, revenue: e.target.value }))} className="w-full rounded-lg px-3 py-2 bg-farm-950 border border-farm-700 text-farm-100 text-sm" />
                  </div>
                  <div>
                    <label className="block text-xs mb-1 text-farm-400">Platform Fees</label>
                    <input type="number" step="0.01" value={editFormData.platform_fees} onChange={(e) => setEditFormData(d => ({ ...d, platform_fees: e.target.value }))} className="w-full rounded-lg px-3 py-2 bg-farm-950 border border-farm-700 text-farm-100 text-sm" />
                  </div>
                  <div>
                    <label className="block text-xs mb-1 text-farm-400">Payment Fees</label>
                    <input type="number" step="0.01" value={editFormData.payment_fees} onChange={(e) => setEditFormData(d => ({ ...d, payment_fees: e.target.value }))} className="w-full rounded-lg px-3 py-2 bg-farm-950 border border-farm-700 text-farm-100 text-sm" />
                  </div>
                  <div>
                    <label className="block text-xs mb-1 text-farm-400">Shipping Charged</label>
                    <input type="number" step="0.01" value={editFormData.shipping_charged} onChange={(e) => setEditFormData(d => ({ ...d, shipping_charged: e.target.value }))} className="w-full rounded-lg px-3 py-2 bg-farm-950 border border-farm-700 text-farm-100 text-sm" />
                  </div>
                </div>
              </div>
              <div>
                <label className="block text-sm font-medium mb-1 text-farm-200">Notes</label>
                <textarea value={editFormData.notes} onChange={(e) => setEditFormData(d => ({ ...d, notes: e.target.value }))} className="w-full rounded-lg px-3 py-2 bg-farm-950 border border-farm-700 text-farm-100 text-sm" rows={2} />
              </div>

              {/* Line Items */}
              <div className="border-t border-farm-800 pt-4">
                <div className="flex justify-between items-center mb-2">
                  <label className="block text-sm font-medium text-farm-200">Line Items</label>
                  <button type="button" onClick={() => setEditItems([...editItems, { product_id: '', quantity: 1, unit_price: '' }])} className="text-xs text-print-400 hover:text-print-300 flex items-center gap-1">
                    <Plus size={14} /> Add Item
                  </button>
                </div>
                {editItems.length === 0 ? (
                  <p className="text-sm text-farm-500">No line items</p>
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
                              if (product?.price) updated[i].unit_price = product.price
                            }
                            setEditItems(updated)
                          }}
                          className="flex-1 rounded-lg px-2 py-1.5 text-sm bg-farm-950 border border-farm-700 text-farm-100"
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
                            className="w-16 rounded-lg px-2 py-1.5 text-sm bg-farm-950 border border-farm-700 text-farm-100"
                            placeholder="Qty"
                          />
                          <input
                            type="number"
                            step="0.01"
                            value={item.unit_price}
                            onChange={(e) => { const updated = [...editItems]; updated[i].unit_price = e.target.value; setEditItems(updated) }}
                            className="w-24 rounded-lg px-2 py-1.5 text-sm bg-farm-950 border border-farm-700 text-farm-100"
                            placeholder="Price"
                          />
                          <button
                            type="button"
                            onClick={() => setEditItems(editItems.filter((_, j) => j !== i))}
                            className="p-2 text-farm-500 hover:text-red-400 hover:bg-red-900/30 rounded-lg transition-colors"
                          >
                            <X size={14} />
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              <div className="flex justify-end gap-2 pt-4 border-t border-farm-800">
                <button type="button" onClick={() => setEditingOrder(null)} className="px-4 py-2 bg-farm-800 hover:bg-farm-700 rounded-lg transition-colors text-sm">Cancel</button>
                <button type="submit" className="px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg transition-colors text-sm">Save Changes</button>
              </div>
            </form>
          </div>
        </div>
      )}

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
