import { useState, useEffect } from 'react'
import { products, models } from '../api'
import { Package, Plus, Trash2, Pencil, X, Save, Layers } from 'lucide-react'

export default function Products() {
  const [productList, setProductList] = useState([])
  const [modelList, setModelList] = useState([])
  const [loading, setLoading] = useState(true)
  const [showModal, setShowModal] = useState(false)
  const [editingProduct, setEditingProduct] = useState(null)
  const [formData, setFormData] = useState({ name: '', sku: '', price: '', description: '' })
  const [components, setComponents] = useState([])
  const [expandedProduct, setExpandedProduct] = useState(null)

  useEffect(() => {
    loadData()
  }, [])

  const loadData = async () => {
    try {
      const [prods, mods] = await Promise.all([products.list(), models.list()])
      setProductList(prods)
      setModelList(mods)
    } catch (err) {
      console.error('Failed to load data:', err)
    } finally {
      setLoading(false)
    }
  }

  const openCreateModal = () => {
    setEditingProduct(null)
    setFormData({ name: '', sku: '', price: '', description: '' })
    setComponents([])
    setShowModal(true)
  }

  const openEditModal = async (product) => {
    setEditingProduct(product)
    setFormData({
      name: product.name,
      sku: product.sku || '',
      price: product.price || '',
      description: product.description || ''
    })
    try {
      const full = await products.get(product.id)
      setComponents(full.components || [])
    } catch (err) {
      setComponents([])
    }
    setShowModal(true)
  }

  const handleSubmit = async (e) => {
    e.preventDefault()
    try {
      const data = {
        name: formData.name,
        sku: formData.sku || null,
        price: formData.price ? parseFloat(formData.price) : null,
        description: formData.description || null
      }

      if (editingProduct) {
        await products.update(editingProduct.id, data)
      } else {
        data.components = components.map(c => ({
          model_id: c.model_id,
          quantity_needed: c.quantity_needed,
          notes: c.notes || null
        }))
        await products.create(data)
      }
      setShowModal(false)
      loadData()
    } catch (err) {
      console.error('Failed to save product:', err)
      alert('Failed to save product')
    }
  }

  const handleDelete = async (id) => {
    if (!confirm('Delete this product?')) return
    try {
      await products.delete(id)
      loadData()
    } catch (err) {
      console.error('Failed to delete:', err)
    }
  }

  const addComponent = () => {
    setComponents([...components, { model_id: '', quantity_needed: 1, notes: '' }])
  }

  const updateComponent = (index, field, value) => {
    const updated = [...components]
    updated[index][field] = field === 'quantity_needed' || field === 'model_id' ? parseInt(value) || '' : value
    setComponents(updated)
  }

  const removeComponent = (index) => {
    setComponents(components.filter((_, i) => i !== index))
  }

  const toggleExpand = async (product) => {
    if (expandedProduct?.id === product.id) {
      setExpandedProduct(null)
    } else {
      const full = await products.get(product.id)
      setExpandedProduct(full)
    }
  }

  if (loading) return <div className="p-6 text-farm-300">Loading...</div>

  return (
    <div className="p-4 md:p-6">
      <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4 mb-6">
        <h1 className="text-xl md:text-2xl font-bold flex items-center gap-2 text-farm-100">
          <Package className="w-5 h-5 md:w-6 md:h-6" />
          Products
        </h1>
        <button
          onClick={openCreateModal}
          className="px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg transition-colors text-sm flex items-center gap-2"
        >
          <Plus size={16} /> New Product
        </button>
      </div>

      {productList.length === 0 ? (
        <div className="text-center py-12 text-farm-500">
          <Package className="w-12 h-12 mx-auto mb-4 opacity-50" />
          <p>No products yet. Create your first product to get started.</p>
        </div>
      ) : (
        <div className="bg-farm-900 rounded-xl border border-farm-800 overflow-hidden">
          {/* Mobile card view */}
          <div className="block md:hidden divide-y divide-farm-800">
            {productList.map(product => (
              <div key={product.id} className="p-4">
                <div className="flex justify-between items-start mb-2">
                  <button
                    onClick={() => toggleExpand(product)}
                    className="font-medium flex items-center gap-1 text-print-400 hover:text-print-300"
                  >
                    {product.component_count > 0 && <Layers className="w-4 h-4" />}
                    {product.name}
                  </button>
                  <div className="flex gap-1">
                    <button 
                      onClick={() => openEditModal(product)} 
                      className="p-1 md:p-1.5 text-farm-400 hover:bg-farm-800 rounded transition-colors"
                    >
                      <Pencil size={14} />
                    </button>
                    <button 
                      onClick={() => handleDelete(product.id)} 
                      className="p-1 md:p-1.5 text-farm-500 hover:text-red-400 hover:bg-red-900/50 rounded transition-colors"
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                </div>
                <div className="text-sm space-y-1 text-farm-400">
                  <div>SKU: {product.sku || '-'}</div>
                  <div>Price: {product.price ? `$${product.price.toFixed(2)}` : '-'}</div>
                  <div>Components: {product.component_count || 0} parts</div>
                  {product.estimated_cogs && <div>Est. COGS: ${product.estimated_cogs.toFixed(2)}</div>}
                </div>
                {expandedProduct?.id === product.id && (
                  <div className="mt-3 pt-3 border-t border-farm-800">
                    <h4 className="font-medium mb-2 text-sm text-farm-200">Bill of Materials</h4>
                    {expandedProduct.components.length === 0 ? (
                      <p className="text-sm text-farm-500">No components.</p>
                    ) : (
                      <ul className="space-y-1">
                        {expandedProduct.components.map(comp => (
                          <li key={comp.id} className="flex items-center gap-2 text-sm text-farm-300">
                            <span className="px-2 py-0.5 rounded bg-print-600/20 text-print-400">{comp.quantity_needed}x</span>
                            <span>{comp.model_name || `Model #${comp.model_id}`}</span>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>

          {/* Desktop table view */}
          <table className="w-full hidden md:table">
            <thead className="bg-farm-950">
              <tr>
                <th className="px-4 py-3 text-left text-sm font-medium text-farm-400">Name</th>
                <th className="px-4 py-3 text-left text-sm font-medium text-farm-400">SKU</th>
                <th className="px-4 py-3 text-left text-sm font-medium text-farm-400">Price</th>
                <th className="px-4 py-3 text-left text-sm font-medium text-farm-400">Components</th>
                <th className="px-4 py-3 text-left text-sm font-medium text-farm-400">Est. COGS</th>
                <th className="px-4 py-3 text-right text-sm font-medium text-farm-400">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-farm-800">
              {productList.map(product => (
                <>
                  <tr key={product.id} className="hover:bg-farm-800/50 transition-colors">
                    <td className="px-4 py-3">
                      <button
                        onClick={() => toggleExpand(product)}
                        className="font-medium flex items-center gap-1 text-print-400 hover:text-print-300"
                      >
                        {product.component_count > 0 && <Layers className="w-4 h-4" />}
                        {product.name}
                      </button>
                    </td>
                    <td className="px-4 py-3 text-farm-400">{product.sku || '-'}</td>
                    <td className="px-4 py-3 text-farm-200">{product.price ? `$${product.price.toFixed(2)}` : '-'}</td>
                    <td className="px-4 py-3">
                      <span className="px-2 py-1 rounded text-sm bg-farm-800 text-farm-300">
                        {product.component_count || 0} parts
                      </span>
                    </td>
                    <td className="px-4 py-3 text-farm-200">
                      {product.estimated_cogs ? `$${product.estimated_cogs.toFixed(2)}` : '-'}
                    </td>
                    <td className="px-4 py-3 text-right">
                      <button 
                        onClick={() => openEditModal(product)} 
                        className="p-1 md:p-1.5 text-farm-400 hover:bg-farm-800 rounded transition-colors"
                        title="Edit"
                      >
                        <Pencil size={14} />
                      </button>
                      <button 
                        onClick={() => handleDelete(product.id)} 
                        className="p-1 md:p-1.5 text-farm-500 hover:text-red-400 hover:bg-red-900/50 rounded transition-colors ml-1"
                        title="Delete"
                      >
                        <Trash2 size={14} />
                      </button>
                    </td>
                  </tr>
                  {expandedProduct?.id === product.id && (
                    <tr>
                      <td colSpan={6} className="px-4 py-3 bg-farm-950">
                        <div className="ml-4">
                          <h4 className="font-medium mb-2 text-farm-200">Bill of Materials</h4>
                          {expandedProduct.components.length === 0 ? (
                            <p className="text-sm text-farm-500">No components. This is a simple product.</p>
                          ) : (
                            <ul className="space-y-1">
                              {expandedProduct.components.map(comp => (
                                <li key={comp.id} className="flex items-center gap-2 text-sm text-farm-300">
                                  <span className="px-2 py-0.5 rounded bg-print-600/20 text-print-400">{comp.quantity_needed}x</span>
                                  <span>{comp.model_name || `Model #${comp.model_id}`}</span>
                                </li>
                              ))}
                            </ul>
                          )}
                        </div>
                      </td>
                    </tr>
                  )}
                </>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Create/Edit Modal */}
      {showModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="bg-farm-900 rounded-xl border border-farm-800 w-full max-w-lg max-h-[90vh] overflow-y-auto">
            <div className="p-4 border-b border-farm-800 flex justify-between items-center">
              <h2 className="text-lg font-semibold text-farm-100">
                {editingProduct ? 'Edit Product' : 'New Product'}
              </h2>
              <button onClick={() => setShowModal(false)} className="p-1 text-farm-500 hover:text-farm-300 hover:bg-farm-800 rounded transition-colors">
                <X size={18} />
              </button>
            </div>
            <form onSubmit={handleSubmit} className="p-4 space-y-4">
              <div>
                <label className="block text-sm font-medium mb-1 text-farm-200">Name *</label>
                <input
                  type="text"
                  value={formData.name}
                  onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                  className="w-full rounded-lg px-3 py-2 bg-farm-950 border border-farm-700 text-farm-100 focus:border-print-500 focus:outline-none"
                  required
                />
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium mb-1 text-farm-200">SKU</label>
                  <input
                    type="text"
                    value={formData.sku}
                    onChange={(e) => setFormData({ ...formData, sku: e.target.value })}
                    className="w-full rounded-lg px-3 py-2 bg-farm-950 border border-farm-700 text-farm-100 focus:border-print-500 focus:outline-none"
                    placeholder="YODA-001"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium mb-1 text-farm-200">Price</label>
                  <input
                    type="number"
                    step="0.01"
                    value={formData.price}
                    onChange={(e) => setFormData({ ...formData, price: e.target.value })}
                    className="w-full rounded-lg px-3 py-2 bg-farm-950 border border-farm-700 text-farm-100 focus:border-print-500 focus:outline-none"
                    placeholder="15.00"
                  />
                </div>
              </div>
              <div>
                <label className="block text-sm font-medium mb-1 text-farm-200">Description</label>
                <textarea
                  value={formData.description}
                  onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                  className="w-full rounded-lg px-3 py-2 bg-farm-950 border border-farm-700 text-farm-100 focus:border-print-500 focus:outline-none"
                  rows={2}
                />
              </div>

              {!editingProduct && (
                <div>
                  <div className="flex justify-between items-center mb-2">
                    <label className="block text-sm font-medium text-farm-200">Components (BOM)</label>
                    <button
                      type="button"
                      onClick={addComponent}
                      className="text-xs text-print-400 hover:text-print-300 flex items-center gap-1"
                    >
                      <Plus size={14} /> Add
                    </button>
                  </div>
                  {components.length === 0 ? (
                    <p className="text-sm text-farm-500">No components = simple single-print product</p>
                  ) : (
                    <div className="space-y-2">
                      {components.map((comp, i) => (
                        <div key={i} className="flex gap-2 items-center">
                          <select
                            value={comp.model_id}
                            onChange={(e) => updateComponent(i, 'model_id', e.target.value)}
                            className="flex-1 rounded-lg px-2 py-1.5 text-sm bg-farm-950 border border-farm-700 text-farm-100"
                            required
                          >
                            <option value="">Select model...</option>
                            {modelList.map(m => (
                              <option key={m.id} value={m.id}>{m.name}</option>
                            ))}
                          </select>
                          <input
                            type="number"
                            min="1"
                            value={comp.quantity_needed}
                            onChange={(e) => updateComponent(i, 'quantity_needed', e.target.value)}
                            className="w-16 rounded-lg px-2 py-1.5 text-sm bg-farm-950 border border-farm-700 text-farm-100"
                            placeholder="Qty"
                          />
                          <button 
                            type="button" 
                            onClick={() => removeComponent(i)} 
                            className="p-2 text-farm-500 hover:text-red-400 hover:bg-red-900/30 rounded transition-colors"
                          >
                            <X size={14} />
                          </button>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              <div className="flex justify-end gap-2 pt-4 border-t border-farm-800">
                <button
                  type="button"
                  onClick={() => setShowModal(false)}
                  className="px-4 py-2 bg-farm-800 hover:bg-farm-700 rounded-lg transition-colors text-sm"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg transition-colors text-sm"
                >
                  {editingProduct ? 'Save Changes' : 'Create Product'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  )
}
