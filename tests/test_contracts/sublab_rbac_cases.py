"""Sublab actions retain independent role/session floors after matrix admission."""
CASES = {
    'products.operations': ('PUT', '/api/products/production/operations/kit', 422),
    'products.materials_manage': ('POST', '/api/consumables/purchases/materials', 422),
    'products.materials_preview': ('POST', '/api/consumables/purchases/email/preview', 422),
    'products.materials_receive': ('POST', '/api/consumables/purchases/lines', 422),
    'products.finishing': ('POST', '/api/products/production/components', 422),
    'products.assortment': ('PUT', '/api/products/production/assortment/999999', 422),
    'products.assortment_preview': ('POST', '/api/products/production/assortment/999999/preview', 404),
    'products.square_manage': ('PUT', '/api/products/square', 422),
    'products.square_send': ('POST', '/api/products/batches/additions/missing/send', 503),
    'products.recipes': ('POST', '/api/products/production/recipes', 422),
    'products.outside_prints': ('POST', '/api/products/production/outside-prints/1/bind', 422),
    'products.inspections': ('POST', '/api/products/production/inspections', 422),
}
