"""Transactional production accounting from immutable scheduled requirements."""
import re
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

_PARAMETER = re.compile(r'(?<!:):([A-Za-z_][A-Za-z0-9_]*)')


class Execution:
    """Use the caller's existing ORM or raw monitor transaction exclusively."""
    def __init__(self,executor):
        self.executor=executor
        self.orm=isinstance(executor,(Session,Connection))
    def execute(self,statement,params=None):
        values=params or {}
        if self.orm:return self.executor.execute(text(statement),values)
        names=[]
        def replace(match):names.append(match.group(1));return '?'
        statement=_PARAMETER.sub(replace,statement)
        return self.executor.cursor().execute(statement,tuple(values[name] for name in names))
    def rows(self,statement,params=None):
        result=self.execute(statement,params);rows=result.fetchall()
        if not rows:return []
        if rows and hasattr(rows[0],'_mapping'):return [dict(r._mapping) for r in rows]
        columns=[r[0] for r in result.description]
        return [dict(zip(columns,row)) for row in rows]
    def one(self,statement,params=None):
        rows=self.rows(statement,params);return rows[0] if rows else None


def _require_active_parent_transaction(executor):
    """Inspect caller physical transaction without acquiring a writer reservation."""
    from core.economic_source_creation import SourceCreationError
    connection = executor
    if isinstance(executor, Session):
        if not executor.in_transaction():
            raise SourceCreationError('Caller physical transaction required')
        connection = executor.connection()
    if isinstance(connection, Connection):
        if not connection.in_transaction():
            raise SourceCreationError('Caller physical transaction required')
        driver = connection.connection.driver_connection
    else:
        raw = getattr(connection, '_connection', connection)
        driver = getattr(raw, 'driver_connection', raw)
    active = (int(driver.info.transaction_status) in (2, 3)
              if hasattr(driver, 'info') else bool(getattr(driver, 'in_transaction', False)))
    if not active:
        raise SourceCreationError('Caller physical transaction required')


def _parent_snapshot(adapter, identifiers):
    from core.order_production import ProductionLinkageChanged
    query = '''SELECT j.id,j.order_item_id,i.order_id AS direct_order,
            b.order_item_id AS bound_item,bi.order_id AS bound_order,s.order_id AS snapshot_order
            FROM jobs j LEFT JOIN order_items i ON i.id=j.order_item_id
            LEFT JOIN order_production_jobs b ON b.job_id=j.id
            LEFT JOIN order_items bi ON bi.id=b.order_item_id
            LEFT JOIN order_production_items s ON s.order_item_id=b.order_item_id WHERE j.id=:id'''
    rows = []
    for identifier in identifiers:
        matches = adapter.rows(query, {'id': identifier})
        if len(matches) != 1:
            raise ProductionLinkageChanged()
        rows.append(tuple(matches[0][key] for key in ('id','order_item_id','direct_order','bound_item','bound_order','snapshot_order')))
    return tuple(rows)


class OrderFulfillmentService:
    def prepare_parent_graph(self, executor, job_ids):
        """Lock orders/items only, then reprove every captured job edge."""
        from core.economic_source_creation import _connection
        from core.order_production import ProductionLinkageChanged, _bounded_parent_job_ids
        connection = _connection(executor)
        dialect = getattr(connection, 'dialect', None)
        raw = getattr(connection, '_connection', connection)
        driver = getattr(raw, 'driver_connection', raw)
        postgres = (dialect.name == 'postgresql' if dialect is not None else hasattr(driver, 'info'))
        adapter = Execution(executor)
        identifiers = _bounded_parent_job_ids(job_ids)
        before = _parent_snapshot(adapter, identifiers)
        orders = tuple(sorted({value for row in before for value in (row[2],row[4],row[5]) if value is not None}))
        items = tuple(sorted({value for row in before for value in (row[1],row[3]) if value is not None}))
        for table, column, values in (('orders', 'id', orders), ('order_items', 'id', items)):
            for identifier in values:
                statement = (f'SELECT {column} FROM {table} WHERE id=:id FOR UPDATE NOWAIT' if postgres
                             else f'UPDATE {table} SET id=id WHERE id=:id RETURNING id')
                if adapter.one(statement, {'id': identifier}) is None:
                    raise ProductionLinkageChanged()
        if _parent_snapshot(adapter, identifiers) != before:
            raise ProductionLinkageChanged()
        return orders, items, before

    def recheck_parent_graph(self, executor, prepared_graph):
        from core.order_production import _validated_parent_graph, ProductionLinkageChanged
        identifiers = _validated_parent_graph(prepared_graph)
        _require_active_parent_transaction(executor)
        if _parent_snapshot(Execution(executor), identifiers) != prepared_graph[2]:
            raise ProductionLinkageChanged()
        return prepared_graph

    def lock_orders(self,executor,order_ids):
        adapter=Execution(executor)
        for identifier in sorted(set(order_ids)):
            # A real parent write takes a lock on SQLite and PostgreSQL.
            result=adapter.execute('UPDATE orders SET status=status WHERE id=:id',{'id':identifier})
            if result.rowcount!=1:raise HTTPException(409,'Production order no longer exists')
        return sorted(set(order_ids))

    def lock_for_jobs(self,executor,job_ids):
        adapter=Execution(executor);parents=set()
        for identifier in sorted(set(job_ids)):
            rows=adapter.rows('''SELECT i.order_id FROM jobs j JOIN order_items i ON i.id=j.order_item_id WHERE j.id=:id
                UNION SELECT s.order_id FROM order_production_jobs b JOIN order_production_items s ON s.order_item_id=b.order_item_id WHERE b.job_id=:id''',{'id':identifier})
            parents.update(r['order_id'] for r in rows)
        return self.lock_orders(executor,parents)

    def assert_job_edit_allowed(self,executor,job_id,changes):
        if not {'model_id','order_item_id','quantity_on_bed','quantity'}.intersection(changes):return
        row=Execution(executor).one('''SELECT b.model_id,b.order_item_id,b.piece_count,j.quantity FROM order_production_jobs b
            LEFT JOIN jobs j ON j.id=b.job_id WHERE b.job_id=:id''',{'id':job_id})
        if not row:return
        expected={'model_id':row['model_id'],'order_item_id':row['order_item_id'],'quantity_on_bed':row['piece_count'],'quantity':1}
        if any(value!=expected[name] for name,value in changes.items() if name in expected):
            raise HTTPException(409,'Scheduled production identity and piece counts cannot be changed')

    def assert_order_structure_edit_allowed(self,executor,order_id):
        self.lock_orders(executor,[order_id]);adapter=Execution(executor)
        if adapter.one('SELECT order_item_id FROM order_production_items WHERE order_id=:id',{'id':order_id}) or adapter.one('SELECT j.id FROM jobs j JOIN order_items i ON i.id=j.order_item_id WHERE i.order_id=:id',{'id':order_id}) or adapter.one('SELECT id FROM consumable_usage WHERE order_id=:id',{'id':order_id}):
            raise HTTPException(409,'Orders with production history cannot change their line items or be deleted')

    def record_item(self,executor,item,requirements,has_consumables):
        adapter=Execution(executor)
        mode=('mixed' if has_consumables else 'printed') if requirements else 'consumable_only'
        adapter.execute('''INSERT INTO order_production_items(order_item_id,order_id,product_id,ordered_quantity,mode,confirmed_quantity)
            VALUES(:item,:order,:product,:quantity,:mode,0)''',{'item':item.id,'order':item.order_id,'product':item.product_id,'quantity':item.quantity,'mode':mode})
        for model_id,pieces in sorted(requirements.items()):
            adapter.execute('INSERT INTO order_production_components(order_item_id,model_id,pieces_per_unit) VALUES(:item,:model,:pieces)',{'item':item.id,'model':model_id,'pieces':pieces})

    def bind_job(self,executor,job):
        adapter = Execution(executor)
        if adapter.execute('UPDATE jobs SET id=id WHERE id=:job', {'job':job.id}).rowcount != 1:
            raise HTTPException(409, 'Production job no longer exists')
        current = adapter.one('SELECT order_item_id,model_id,quantity_on_bed FROM jobs WHERE id=:job', {'job':job.id})
        if current is None or any(current[name] != getattr(job,name) for name in ('order_item_id','model_id','quantity_on_bed')):
            raise HTTPException(409, 'Production job identity changed')
        adapter.execute('INSERT INTO order_production_jobs(job_id,order_item_id,model_id,piece_count) VALUES(:job,:item,:model,:pieces)',{'job':job.id,'item':job.order_item_id,'model':job.model_id,'pieces':job.quantity_on_bed})

    def reconcile_orders(self,executor,order_ids):
        if isinstance(executor,Session):executor.flush()
        adapter=Execution(executor)
        for identifier in sorted(set(order_ids)):
            order=adapter.one('SELECT id,status FROM orders WHERE id=:id',{'id':identifier})
            if not order:raise HTTPException(409,'Production order no longer exists')
            snapshots=adapter.rows('''SELECT s.*,i.quantity AS current_quantity,i.product_id AS current_product,i.order_id AS current_order
                FROM order_production_items s LEFT JOIN order_items i ON i.id=s.order_item_id WHERE s.order_id=:id''',{'id':identifier})
            if not snapshots:continue  # Historical production cannot be reconstructed.
            for snapshot in snapshots:
                if (snapshot['current_quantity']!=snapshot['ordered_quantity'] or snapshot['current_product']!=snapshot['product_id'] or snapshot['current_order']!=identifier):
                    raise HTTPException(409,'Production line item no longer matches its scheduled requirements')
                if snapshot['mode'] in ('historical','consumable_only'):
                    fulfilled=snapshot['confirmed_quantity']
                else:
                    components=adapter.rows('''SELECT c.model_id,c.pieces_per_unit,COALESCE(SUM(CASE WHEN j.status='completed'
                        AND j.model_id=b.model_id AND j.order_item_id=b.order_item_id AND j.quantity_on_bed=b.piece_count AND j.quantity=1
                        THEN b.piece_count ELSE 0 END),0) AS completed_pieces
                        FROM order_production_components c LEFT JOIN order_production_jobs b ON b.order_item_id=c.order_item_id AND b.model_id=c.model_id
                        LEFT JOIN jobs j ON j.id=b.job_id WHERE c.order_item_id=:item GROUP BY c.model_id,c.pieces_per_unit''',{'item':snapshot['order_item_id']})
                    if not components:raise HTTPException(409,'Printed production requirements are missing')
                    fulfilled=min(snapshot['ordered_quantity'],min(c['completed_pieces']//c['pieces_per_unit'] for c in components))
                adapter.execute('UPDATE order_items SET fulfilled_quantity=:quantity WHERE id=:id',{'quantity':fulfilled,'id':snapshot['order_item_id']})
            if order['status'] not in ('cancelled','shipped'):
                items=adapter.rows('SELECT quantity,fulfilled_quantity FROM order_items WHERE order_id=:id',{'id':identifier})
                complete=bool(items) and all(i['quantity']>0 and i['fulfilled_quantity']>=i['quantity'] for i in items)
                adapter.execute('UPDATE orders SET status=:status WHERE id=:id',{'status':'fulfilled' if complete else 'in_progress','id':identifier})

    def confirm_item(self,executor,order_id,item_id,quantity):
        self.lock_orders(executor,[order_id]);adapter=Execution(executor)
        order=adapter.one('SELECT status FROM orders WHERE id=:id',{'id':order_id})
        if order['status'] in ('shipped','cancelled'):raise HTTPException(409,'Shipped or cancelled production cannot be confirmed')
        item=adapter.one('SELECT id,quantity,product_id FROM order_items WHERE id=:item AND order_id=:order',{'item':item_id,'order':order_id})
        if not item:raise HTTPException(404,'Order item not found')
        if item['quantity'] <= 0 or isinstance(quantity,bool) or not isinstance(quantity,int) or not 0<=quantity<=item['quantity']:
            raise HTTPException(400,'Confirmed quantity must be within the ordered quantity')
        snapshot=adapter.one('SELECT mode,confirmed_quantity FROM order_production_items WHERE order_item_id=:id',{'id':item_id})
        if snapshot and snapshot['mode'] not in ('historical','consumable_only'):
            raise HTTPException(409,'Printed production is fulfilled from its original jobs')
        if not snapshot:
            adapter.execute('''INSERT INTO order_production_items(order_item_id,order_id,product_id,ordered_quantity,mode,confirmed_quantity)
                VALUES(:item,:order,:product,:quantity,'historical',0)''',{'item':item_id,'order':order_id,'product':item['product_id'],'quantity':item['quantity']})
        previous=snapshot['confirmed_quantity'] if snapshot else 0
        adapter.execute('UPDATE order_production_items SET confirmed_quantity=:quantity WHERE order_item_id=:id',{'quantity':quantity,'id':item_id})
        self.reconcile_orders(executor,[order_id])
        return snapshot is None or previous!=quantity
