"""All-or-nothing local production scan and same-owner reference validation."""
from pydantic import BaseModel, TypeAdapter, ValidationError
from sqlalchemy import select, text

from core.models import SystemConfig
from modules.orders import assortment, batch_production as batches, replenishment_schemas as shapes

LIMIT = 1000


class ScanProblem(ValueError):
    pass


def problem(kind):
    raise ScanProblem(f'{kind} scan could not be completed. Review the saved production records before planning more stock.')


def parse(schema, raw, kind):
    try:
        return schema.model_validate_json(raw) if isinstance(raw, str) else schema.model_validate(raw)
    except (ValidationError, ValueError, TypeError):
        problem(kind)


class Ownership:
    """Existence and exact tenant ownership; these checks do not decide visibility."""
    def __init__(self, db, owner):
        self.db, self.owner, self.checked = db, owner, set()

    def require(self, kind, identifier):
        if (kind, identifier) in self.checked:
            return
        if kind == 'recipe':
            valid = isinstance(identifier, str) and len(identifier) == 32
        else:
            valid = type(identifier) is int and 0 < identifier <= 9223372036854775807
        if not valid:
            problem('Production reference')
        queries = {
            'job': 'SELECT id,model_id,print_file_id,printer_id,payload_sha256,plate_index,quantity FROM jobs WHERE id=:id AND ((:owner=0 AND charged_to_org_id IS NULL) OR (:owner>0 AND charged_to_org_id=:owner))',
            'model': 'SELECT print_file_id FROM models WHERE id=:id AND ((:owner=0 AND org_id IS NULL) OR (:owner>0 AND org_id=:owner))',
            'file': 'SELECT model_id FROM print_files WHERE id=:id AND ((:owner=0 AND org_id IS NULL) OR (:owner>0 AND org_id=:owner))',
            'product': 'SELECT id FROM products WHERE id=:id AND ((:owner=0 AND org_id IS NULL) OR (:owner>0 AND org_id=:owner))',
            'printer': 'SELECT id FROM printers WHERE id=:id AND ((:owner=0 AND org_id IS NULL) OR (:owner>0 AND org_id=:owner))',
            'recipe': 'SELECT id FROM production_recipes WHERE id=:id AND owner=:owner',
            'print job': '''SELECT p.scheduled_job_id,p.printer_id FROM print_jobs p JOIN printers r ON r.id=p.printer_id
                WHERE p.id=:id AND ((:owner=0 AND r.org_id IS NULL) OR (:owner>0 AND r.org_id=:owner))''',
        }
        row = self.db.execute(text(queries[kind]), {'id': identifier, 'owner': self.owner}).mappings().first()
        if row is None:
            problem(kind.capitalize() + ' ownership')
        self.checked.add((kind, identifier))
        if kind == 'job':
            self.references(parse(shapes.Work, dict(row), 'Job'))
        elif kind == 'model' and row['print_file_id'] is not None:
            self.require('file', row['print_file_id'])
        elif kind == 'file' and row['model_id'] is not None:
            self.require('model', row['model_id'])
        elif kind == 'print job':
            self.require('printer', row['printer_id'])
            if row['scheduled_job_id'] is not None:
                self.require('job', row['scheduled_job_id'])

    def physical(self, uid, *, job_id=None, print_job_id=None):
        rows = self.db.execute(text('SELECT id,scheduled_job_id FROM print_jobs WHERE economic_birth_nonce=:uid'), {'uid': uid}).all()
        if len(rows) != 1:
            problem('Print identity')
        identifier, linked = rows[0]
        self.require('print job', identifier)
        if (job_id is not None and linked != job_id) or (print_job_id is not None and identifier != print_job_id):
            problem('Print identity')

    def references(self, value):
        if isinstance(value, BaseModel):
            fields = {'job_id': 'job', 'reference_job_id': 'job', 'model_id': 'model', 'bom_model_id': 'model',
                      'print_file_id': 'file', 'product_id': 'product', 'printer_id': 'printer',
                      'print_job_id': 'print job', 'recipe_id': 'recipe'}
            for key in type(value).model_fields:
                item = getattr(value, key)
                if key in fields and item is not None:
                    self.require(fields[key], item)
                else:
                    self.references(item)
            if isinstance(value, shapes.OutsideBinding) and value.owner != self.owner:
                problem('Outside print ownership')
            if isinstance(value, shapes.BOM):
                for key in value.components.keys() | value.sources.keys():
                    self.require('model', int(key))
                if any(str(source.model_id) != key for key, source in value.sources.items()):
                    problem('Finishing model')
        elif isinstance(value, dict):
            for item in value.values():
                self.references(item)
        elif isinstance(value, list):
            for item in value:
                self.references(item)


def load_recipe(db, owner, identifier, ownership=None):
    raw = db.execute(select(batches.recipes.c.definition).where(
        batches.recipes.c.id == identifier, batches.recipes.c.owner == owner)).scalar_one_or_none()
    if raw is None:
        problem('Recipe ownership')
    return recipe_definition(identifier, raw, ownership or Ownership(db, owner))


def recipe_definition(identifier, raw, ownership):
    try:
        TypeAdapter(shapes.RecordID).validate_python(identifier)
    except ValidationError:
        problem('Recipe identity')
    definition = parse(shapes.Recipe, raw, 'Recipe')
    ownership.references(definition)
    outside = definition.outside_print_evidence
    ownership.physical(definition.reference_observation_uid,
                       print_job_id=outside.print_job_id if outside else None)
    # The column is authoritative even when a retained copy includes another id.
    return {**definition.model_dump(exclude_none=True), 'id': identifier}


def scan(db, owner):
    """Return recipes and counts only after every used definition and reference passes."""
    ownership = Ownership(db, owner)
    raw = db.execute(select(batches.recipes.c.id, batches.recipes.c.definition).where(
        batches.recipes.c.owner == owner).limit(LIMIT + 1)).all()
    if len(raw) > LIMIT:
        problem('Recipe')
    recipes = [recipe_definition(rid, definition, ownership) for rid, definition in raw]
    from modules.jobs.models import Job
    from modules.models_library.models import Model
    from sqlalchemy import literal_column
    clause = Job.charged_to_org_id.is_(None) if owner == 0 else Job.charged_to_org_id == owner
    # Known unrelated models do not affect this plan. Missing or nonlocal models
    # cannot prove work unrelated and must reach the strict ownership check.
    model_scope = Model.org_id.is_(None) if owner == 0 else Model.org_id == owner
    own_model = select(Model.id).where(Model.id == Job.model_id, model_scope).exists()
    models = {recipe['payload']['model_id'] for recipe in recipes}
    relevant = Job.model_id.in_(models) | Job.model_id.is_(None) | (Job.model_id <= 0) | ~own_model
    jobs = db.execute(select(Job, literal_column('jobs.economic_uid')).where(
        clause, relevant, Job.status.in_(assortment.WORK_STATES)).order_by(Job.id.desc()).limit(LIMIT + 1)).all()
    if len(jobs) > LIMIT:
        problem('Matching-job')
    for job, _ in jobs:
        ownership.require('job', job.id)
    outstanding = db.execute(select(batches.additions.c.receipt_id).where(
        batches.additions.c.owner == owner, batches.additions.c.state.in_(('prepared', 'unknown'))).limit(LIMIT + 1)).scalars().all()
    if len(outstanding) > LIMIT:
        problem('Pending-addition')
    # Validate receipts before allowing their job UID to suppress completed work.
    uids = [uid for _, uid in jobs if uid]
    receipt_rows = db.execute(select(batches.receipts).where(batches.receipts.c.owner == owner,
        (batches.receipts.c.id.in_(outstanding)) | (batches.receipts.c.job_uid.in_(uids))).limit(LIMIT + 1)).mappings().all()
    if len(receipt_rows) > LIMIT:
        problem('Receipt')
    receipts, inspected = {}, set()
    for row in receipt_rows:
        detail = parse(shapes.Receipt, row['definition'], 'Receipt')
        # Ignore shadow columns; validate only the authoritative recipe reference.
        detail.recipe_id = row['recipe_id']
        ownership.references(detail)
        if detail.job_uid != row['job_uid']:
            problem('Receipt job identity')
        if detail.job_id is not None:
            uid = db.execute(text('SELECT economic_uid FROM jobs WHERE id=:id AND ((:owner=0 AND charged_to_org_id IS NULL) OR (:owner>0 AND charged_to_org_id=:owner))'),
                             {'id': detail.job_id, 'owner': owner}).scalar()
            if uid != detail.job_uid:
                problem('Receipt job identity')
        ownership.physical(detail.physical_uid, job_id=detail.job_id, print_job_id=detail.print_job_id)
        receipts[row['id']] = detail
        inspected.add(row['job_uid'])
    missing = set(outstanding) - receipts.keys()
    if missing:
        from modules.orders.finishing import key
        raw = db.execute(select(SystemConfig.value).where(SystemConfig.key == key(owner))).scalar_one_or_none()
        if raw is None:
            problem('Pending receipt')
        finishing = parse(shapes.Finishing, raw, 'Finishing receipt')
        if finishing.owner != owner:
            problem('Finishing ownership')
        ownership.references(finishing)
        for rid, component in finishing.components.items():
            if rid != component.id:
                problem('Component identity')
            ownership.physical(component.physical_uid, job_id=component.job_id)
        for finish in finishing.finishes.values():
            for allocation in finish.allocations:
                if allocation.receipt_id not in finishing.components:
                    problem('Finishing allocation')
            if finish.id in receipts:
                problem('Receipt identity')
            receipts[finish.id] = finish
        if missing - receipts.keys():
            problem('Pending receipt')
    coming = {}
    def entry(pid):
        return coming.setdefault(pid, {'pieces': 0, 'job_ids': [], 'awaiting_stock': 0})
    for rid in outstanding:
        for output in receipts[rid].outputs:
            pieces = output.send_quantity if output.send_quantity is not None else output.accepted
            entry(output.product_id)['pieces'] += pieces
            entry(output.product_id)['awaiting_stock'] += pieces
    for job, uid in jobs:
        if str(getattr(job.status, 'value', job.status)) == 'completed' and uid in inspected:
            continue
        makes = {}
        for recipe in recipes:
            if not assortment.job_may_produce(job, recipe['payload']):
                continue
            for output in recipe['outputs']:
                if output['kind'] == 'main':
                    makes[output['product_id']] = max(makes.get(output['product_id'], 0), output['expected'] * (job.quantity or 1))
        for pid, pieces in makes.items():
            entry(pid)['pieces'] += pieces
            entry(pid)['job_ids'].append(job.id)
    return recipes, coming
