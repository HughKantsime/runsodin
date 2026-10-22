"""Fixed-host, read-only Square transport. No stock/catalog/payment mutations."""
import json
import re
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

import requests
from fastapi import HTTPException

VERSION = '2026-09-16'
HOSTS = {'sandbox': 'https://connect.squareupsandbox.com', 'production': 'https://connect.squareup.com'}
ID = re.compile(r'^[A-Za-z0-9_-]{1,192}$')


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise HTTPException(400, 'Invalid Square identifier')
    return value


def invalid():
    raise HTTPException(502, 'Square returned an incomplete or invalid response. No stock was inferred.')


def present(obj, location):
    if not isinstance(obj.get('absent_at_location_ids', []), list) or not isinstance(obj.get('present_at_location_ids', []), list):
        invalid()
    if obj.get('is_deleted') is True:
        return False
    if location in obj.get('absent_at_location_ids', []):
        return False
    return obj.get('present_at_all_locations') is True or location in obj.get('present_at_location_ids', [])


def tracked(data, location):
    if not isinstance(data, dict):
        invalid()
    overrides = data.get('location_overrides', [])
    if not isinstance(overrides, list):
        invalid()
    for override in overrides:
        if not isinstance(override, dict):
            invalid()
        if override.get('location_id') == location and 'track_inventory' in override:
            return override['track_inventory'] is True
    return data.get('track_inventory') is True


class SquareReader:
    def __init__(self, environment, token):
        if environment not in HOSTS:
            raise HTTPException(400, 'Choose a supported Square environment')
        self.host, self.token = HOSTS[environment], token

    def _read(self, method, path, *, params=None, body=None):
        allowed = (method == 'GET' and (path in ('/v2/merchants/me', '/v2/locations', '/v2/catalog/list')
                   or re.fullmatch(r'/v2/catalog/object/[A-Za-z0-9_-]{1,192}', path)))
        if not allowed and not (method == 'POST' and path in ('/v2/inventory/counts/batch-retrieve', '/oauth2/token/status')):
            raise HTTPException(400, 'Square operation is not a supported read')
        try:
            with requests.Session() as session:
                session.trust_env = False
                with session.request(method, self.host + path, params=params, json=body,
                        headers={'Authorization': 'Bearer ' + self.token, 'Square-Version': VERSION},
                        timeout=(4, 10), allow_redirects=False, stream=True) as response:
                    if response.status_code in (401, 403):
                        raise HTTPException(502, 'Square denied access. Verify the token, environment and required read permissions.')
                    if response.status_code == 429:
                        raise HTTPException(503, 'Square rate limit reached. Wait before trying again.')
                    if response.status_code != 200:
                        raise HTTPException(502, 'Square read failed. No stock was changed. Try again.')
                    chunks, size, deadline = [], 0, time.monotonic() + 15
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > 4 * 1024 * 1024 or time.monotonic() > deadline:
                            invalid()
                        chunks.append(chunk)
                    result = json.loads(b''.join(chunks))
                    if not isinstance(result, dict) or result.get('errors'):
                        invalid()
                    return result
        except HTTPException:
            raise
        except (requests.RequestException, ValueError, TypeError):
            raise HTTPException(502, 'Square is unavailable or returned invalid data. No stock was inferred.') from None

    def require_write_scope(self):
        scopes = self._read('POST', '/oauth2/token/status', body={}).get('scopes')
        if (not isinstance(scopes, list) or not scopes or len(scopes) > 200
                or any(not isinstance(v, str) or not v or len(v) > 100 for v in scopes)
                or 'INVENTORY_WRITE' not in scopes):
            raise HTTPException(409, 'Square token scope check requires INVENTORY_WRITE')

    def merchant(self):
        merchant = self._read('GET', '/v2/merchants/me').get('merchant')
        if not isinstance(merchant, dict) or merchant.get('status') != 'ACTIVE':
            invalid()
        return identifier(merchant.get('id'))

    def locations(self, merchant):
        values = self._read('GET', '/v2/locations').get('locations', [])
        if not isinstance(values, list):
            invalid()
        return [{'id': identifier(v.get('id')), 'name': str(v.get('name') or v.get('id'))[:200]}
                for v in values if isinstance(v, dict) and v.get('status') == 'ACTIVE' and v.get('merchant_id') == merchant]

    def catalog(self, cursor=None):
        params = {'types': 'ITEM'}
        if cursor:
            if not isinstance(cursor, str) or len(cursor) > 4096:
                raise HTTPException(400, 'Invalid catalog page')
            params['cursor'] = cursor
        result = self._read('GET', '/v2/catalog/list', params=params)
        values, items = result.get('objects', []), []
        if not isinstance(values, list):
            invalid()
        for item in values:
            if not isinstance(item, dict) or item.get('type') != 'ITEM' or item.get('is_deleted'):
                continue
            data = item.get('item_data', {})
            if not isinstance(data, dict) or not isinstance(data.get('variations', []), list):
                invalid()
            for variation in data.get('variations', []):
                if not isinstance(variation, dict) or variation.get('is_deleted') or variation.get('type') != 'ITEM_VARIATION':
                    continue
                vd = variation.get('item_variation_data', {})
                if not isinstance(vd, dict):
                    invalid()
                items.append({'variation_id': identifier(variation.get('id')), 'item_name': str(data.get('name') or 'Unnamed item')[:200],
                              'variation_name': str(vd.get('name') or 'Default')[:200], 'sku': str(vd['sku'])[:100] if vd.get('sku') else None})
        cursor = result.get('cursor')
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 4096):
            invalid()
        return {'items': items, 'cursor': cursor or None}

    def variation(self, variation_id, location):
        result = self._read('GET', '/v2/catalog/object/' + quote(identifier(variation_id), safe=''), params={'include_related_objects': 'true'})
        obj = result.get('object')
        if not isinstance(obj, dict) or obj.get('id') != variation_id or obj.get('type') != 'ITEM_VARIATION' or not present(obj, location):
            raise HTTPException(409, 'The Square variation is deleted or unavailable at this location. Review its mapping.')
        data = obj.get('item_variation_data', {})
        if not isinstance(data, dict) or not isinstance(result.get('related_objects', []), list):
            invalid()
        parent_id = identifier(data.get('item_id'))
        parent = next((v for v in result.get('related_objects', []) if isinstance(v, dict) and v.get('id') == parent_id), None)
        if parent is None:
            parent = self._read('GET', '/v2/catalog/object/' + parent_id).get('object')
        if not isinstance(parent, dict) or parent.get('id') != parent_id or parent.get('type') != 'ITEM' or not present(parent, location):
            raise HTTPException(409, 'The Square parent item is unavailable at this location. Review its mapping.')
        if not isinstance(parent.get('item_data', {}), dict):
            invalid()
        return {'item_name': str(parent.get('item_data', {}).get('name') or 'Unnamed item')[:200],
                'variation_name': str(data.get('name') or 'Default')[:200], 'tracked': tracked(data, location)}

    def count(self, variation, location):
        result = self._read('POST', '/v2/inventory/counts/batch-retrieve', body={
            'catalog_object_ids': [identifier(variation)], 'location_ids': [identifier(location)], 'states': ['IN_STOCK']})
        if result.get('cursor'):
            invalid()
        counts = result.get('counts', [])
        if not isinstance(counts, list):
            invalid()
        matches = [v for v in counts if isinstance(v, dict) and v.get('catalog_object_id') == variation
                   and v.get('location_id') == location and v.get('state') == 'IN_STOCK']
        if not matches:
            return {'quantity': None, 'calculated_at': None, 'state': 'unknown'}
        if len(matches) != 1:
            invalid()
        row = matches[0]
        try:
            quantity = row['quantity']
            if not isinstance(quantity, str) or len(quantity) > 64 or not Decimal(quantity).is_finite():
                invalid()
        except (KeyError, InvalidOperation):
            invalid()
        return {'quantity': quantity, 'calculated_at': row.get('calculated_at'), 'state': 'known'}
