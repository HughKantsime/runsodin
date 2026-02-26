import { Eye, Pencil, Trash2, Play, Truck, Ban } from 'lucide-react'
import { canDo } from '../../permissions'

export default function OrderTable({ orders, STATUS_CLASSES, onDetail, onEdit, onSchedule, onShip, onCancel, onDelete }) {
  return (
    <div className="bg-farm-900 rounded-lg border border-farm-800 overflow-hidden">
      {/* Mobile card view */}
      <div className="block md:hidden divide-y divide-farm-800">
        {orders.map(order => (
          <div key={order.id} className="p-4">
            <div className="flex justify-between items-start mb-2">
              <button
                onClick={() => onDetail(order)}
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
              <button onClick={() => onDetail(order)} className="p-1 md:p-1.5 text-print-400 hover:bg-print-900/50 rounded-lg transition-colors" title="View Details">
                <Eye size={14} />
              </button>
              {canDo('orders.edit') && (
                <button onClick={() => onEdit(order)} className="p-1 md:p-1.5 text-farm-400 hover:bg-farm-800 rounded-lg transition-colors" title="Edit Order">
                  <Pencil size={14} />
                </button>
              )}
              {canDo('orders.edit') && order.status === 'pending' && (
                <button onClick={() => onSchedule(order.id)} className="p-1 md:p-1.5 text-print-400 hover:bg-print-900/50 rounded-lg transition-colors" title="Schedule Jobs">
                  <Play size={14} />
                </button>
              )}
              {canDo('orders.ship') && order.status === 'fulfilled' && (
                <button onClick={() => onShip(order)} className="p-1 md:p-1.5 text-print-400 hover:bg-print-900/50 rounded-lg transition-colors" title="Mark Shipped">
                  <Truck size={14} />
                </button>
              )}
              {canDo('orders.edit') && (order.status === 'pending' || order.status === 'in_progress') && (
                <button onClick={() => onCancel(order)} className="p-1 md:p-1.5 text-farm-500 hover:text-red-400 hover:bg-red-900/50 rounded-lg transition-colors" title="Cancel Order">
                  <Ban size={14} />
                </button>
              )}
              {canDo('orders.delete') && (
                <button onClick={() => onDelete(order.id)} className="p-1 md:p-1.5 text-farm-500 hover:text-red-400 hover:bg-red-900/50 rounded-lg transition-colors" title="Delete">
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
          {orders.map(order => (
            <tr key={order.id} className="hover:bg-farm-800/50 transition-colors">
              <td className="px-4 py-3">
                <button onClick={() => onDetail(order)} className="font-medium text-print-400 hover:text-print-300">
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
                <button onClick={() => onDetail(order)} className="p-1 md:p-1.5 text-print-400 hover:bg-print-900/50 rounded-lg transition-colors" title="View Details">
                  <Eye size={14} />
                </button>
                {canDo('orders.edit') && (
                  <button onClick={() => onEdit(order)} className="p-1 md:p-1.5 text-farm-400 hover:bg-farm-800 rounded-lg transition-colors" title="Edit Order">
                    <Pencil size={14} />
                  </button>
                )}
                {canDo('orders.edit') && order.status === 'pending' && (
                  <button onClick={() => onSchedule(order.id)} className="p-1 md:p-1.5 text-print-400 hover:bg-print-900/50 rounded-lg transition-colors" title="Schedule Jobs">
                    <Play size={14} />
                  </button>
                )}
                {canDo('orders.ship') && order.status === 'fulfilled' && (
                  <button onClick={() => onShip(order)} className="p-1 md:p-1.5 text-print-400 hover:bg-print-900/50 rounded-lg transition-colors" title="Mark Shipped">
                    <Truck size={14} />
                  </button>
                )}
                {canDo('orders.edit') && (order.status === 'pending' || order.status === 'in_progress') && (
                  <button onClick={() => onCancel(order)} className="p-1 md:p-1.5 text-farm-500 hover:text-red-400 hover:bg-red-900/50 rounded-lg transition-colors" title="Cancel Order">
                    <Ban size={14} />
                  </button>
                )}
                {canDo('orders.delete') && (
                  <button onClick={() => onDelete(order.id)} className="p-1 md:p-1.5 text-farm-500 hover:text-red-400 hover:bg-red-900/50 rounded-lg transition-colors" title="Delete">
                    <Trash2 size={14} />
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
