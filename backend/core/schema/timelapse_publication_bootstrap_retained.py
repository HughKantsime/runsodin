"""Streaming proof of the retained publication history for bootstrap."""
from itertools import groupby, islice
from operator import itemgetter
import stat

from sqlalchemy import BigInteger, and_, case, cast, func, inspect, select, text, union

from core.schema.migrator import MigrationError
from core.timelapse_manifest import MAX_OBJECTS, _parts
from core.timelapse_ownership_schema import anchors
from core.timelapse_publication_schema import (
    TABLES, bindings, facts, lifetimes, namespaces, publications, retained, versions,
)
from core.timelapse_publication_validation import (
    EMPTY, FILE, PROVENANCE, STABLE, digest, directory, file_fact, number, parent_paths, refuse,
)
from core.timelapse_recipes import recipe_from_row
from core.timelapse_recovery_schema import moves
from core.timelapse_rename import _component
from core.timelapse_retirement_schema import intent, objects
from core.timelapse_unlink_schema import unlinks


def _rows(db, statement):
    # A server-side cursor on PostgreSQL; SQLite steps the local cursor. Convert
    # each typed row once rather than reprocessing its values for every lookup.
    with db.execute(statement.execution_options(yield_per=512)) as result:
        keys = tuple(result.keys())
        for row in result:
            yield dict(zip(keys, row))


def _lookup(db, column, argument):
    # A pinned lookup binds a string, so the searched column supplies the
    # collation. SQLite already prefers the left column. On PostgreSQL the
    # cstring returned by textout has no collation; textin restores the same
    # text with the implicit default collation of a bound parameter.
    if db.dialect.name == 'postgresql':
        argument = func.pg_catalog.textin(func.pg_catalog.textout(argument))
    return column == argument


def _result_counts():
    return (select(publications.c.result_nonce, func.count().label('count'))
            .where(publications.c.kind != 'frame').group_by(publications.c.result_nonce).subquery())


def _authority(db):
    # 019 accepts adopted key collations. SQL finds the row using the table's
    # collation, but the pinned Python proof compares the *requested* nonce
    # exactly. Keep differently spelled lookup arguments distinct.
    postgres = db.dialect.name == 'postgresql'
    exact = 'C' if postgres else 'BINARY'

    def requested(column):
        key = func.convert_to(column, text("'UTF8'")) if postgres else column.collate(exact)
        return select(key.label('key'))

    def arguments(name, *queries):
        keys = union(*queries).subquery()
        # Decode after UNION so a forced PostgreSQL collation does not leak
        # into equality against an adopted table's own collation.
        nonce = func.convert_from(keys.c.key, text("'UTF8'")) if postgres else keys.c.key
        return select(nonce.label('nonce')).cte(name)

    version_keys = arguments(
        'version_keys', requested(versions.c.version_nonce), requested(publications.c.source_nonce),
        requested(publications.c.result_nonce), requested(retained.c.version_nonce),
        requested(retained.c.retire_operation_id).where(retained.c.state.collate(exact) == 'retired'),
    )
    life_keys = arguments('life_keys', requested(lifetimes.c.lifetime_nonce), requested(versions.c.lifetime_nonce),
                          requested(namespaces.c.lifetime_nonce))
    owner_keys = arguments('owner_keys', requested(version_keys.c.nonce), requested(life_keys.c.nonce))
    referenced = select(anchors, owner_keys.c.nonce).select_from(
        owner_keys.outerjoin(anchors, _lookup(db, anchors.c.operation_id, owner_keys.c.nonce)))
    for owner in _rows(db, referenced):
        nonce = owner['nonce']
        _component(nonce)
        if owner['owner_state'] not in ('known', 'shared'):
            refuse()
        _parts(owner['filename'])
        for key in ('timelapse_id', 'printer_id', 'scheduled_job_id', 'print_file_id'):
            number(owner[key], 1)
        if owner['owner_state'] == 'known':
            number(owner['owner_org_id'], 1)
        for key in ('education_submission_id', 'capture_printer_org_id'):
            if owner[key] is not None:
                number(owner[key], 1)

    initial = (
        life_keys.outerjoin(lifetimes, _lookup(db, lifetimes.c.lifetime_nonce, life_keys.c.nonce))
        .outerjoin(versions, _lookup(db, versions.c.version_nonce, life_keys.c.nonce))
        .outerjoin(anchors, _lookup(db, anchors.c.operation_id, life_keys.c.nonce))
    )
    for root in _rows(db, select(life_keys.c.nonce, lifetimes, versions.c.lifetime_nonce.label('initial_life'),
                                 anchors.c.timelapse_id, anchors.c.printer_id).select_from(initial)):
        if (root['initial_life'] != root['nonce'] or root['original_timelapse_id'] != root['timelapse_id']
                or root['original_printer_id'] != root['printer_id']):
            refuse()
        for key in STABLE:
            number(root['root_' + key], int(key == 'inode'))
        if (root['root_mode'] & 0o170000) != stat.S_IFDIR or root['root_mode'] & 0o022:
            refuse()

    base = anchors.alias('base')
    counts = _result_counts()
    version_graph = (
        version_keys.outerjoin(versions, _lookup(db, versions.c.version_nonce, version_keys.c.nonce))
        .outerjoin(anchors, _lookup(db, anchors.c.operation_id, version_keys.c.nonce))
        .outerjoin(lifetimes, _lookup(db, lifetimes.c.lifetime_nonce, versions.c.lifetime_nonce))
        .outerjoin(base, _lookup(db, base.c.operation_id, versions.c.lifetime_nonce))
        .outerjoin(counts, _lookup(db, counts.c.result_nonce, version_keys.c.nonce))
    )
    identity = ('timelapse_id', *PROVENANCE)
    statement = select(version_keys.c.nonce, versions, lifetimes.c.lifetime_nonce.label('root'), counts.c.count,
                       *(anchors.c[k] for k in identity), *(base.c[k].label('base_' + k) for k in identity)).select_from(version_graph)
    for row in _rows(db, statement):
        if row['root'] is None or row['timelapse_id'] is None or any(row[k] != row['base_' + k] for k in identity):
            refuse()
        if row['nonce'] != row['lifetime_nonce'] and row['count'] != 1:
            refuse()

    namespace_graph = namespaces.outerjoin(lifetimes, _lookup(db, lifetimes.c.lifetime_nonce, namespaces.c.lifetime_nonce))
    statement = select(namespaces, *(lifetimes.c['root_' + k] for k in STABLE)).select_from(namespace_graph)
    for space in _rows(db, statement):
        name = space['namespace_id']
        _component(name)
        if space['root_inode'] is None:
            refuse()
        if space['relative_path'] != 'frames_' + name:
            refuse()
        _component(space['relative_path'])
        present = [space[k] is not None for k in STABLE]
        if any(present):
            if not all(present):
                refuse()
            directory(dict(space, kind='directory', link_count=None, byte_count=None, mtime_ns=None,
                           ctime_ns=None, digest_sha256=None), space, private=True)
            if space['inode'] == space['root_inode'] or space['phase'] == 'planned':
                refuse()
        elif space['phase'] not in ('planned', 'blocked'):
            refuse()

    binding_graph = bindings.outerjoin(namespaces, _lookup(db, namespaces.c.namespace_id, bindings.c.namespace_id))
    statement = select(bindings, namespaces.c.lifetime_nonce.label('space_life'), namespaces.c.phase).select_from(binding_graph)
    for binding in _rows(db, statement):
        number(binding['next_number'], 1)
        if binding['space_life'] != binding['lifetime_nonce'] or binding['phase'] not in ('bound', 'retired'):
            refuse()


def _operations(db):
    result = anchors.alias('result')
    result_version = versions.alias('result_version')
    result_root = lifetimes.alias('result_root')
    counts = _result_counts()
    op = publications.c
    # Outer joins keep a broken link visible to the proof. Each join is on a
    # unique key, so the application holds one operation regardless of the
    # number of distinct lifetimes, edits or retained versions in the journal.
    graph = (
        publications.outerjoin(versions, _lookup(db, versions.c.version_nonce, op.source_nonce))
        .outerjoin(result_version, _lookup(db, result_version.c.version_nonce, op.result_nonce))
        .outerjoin(anchors, _lookup(db, anchors.c.operation_id, op.source_nonce))
        .outerjoin(result, _lookup(db, result.c.operation_id, op.result_nonce))
        .outerjoin(lifetimes, _lookup(db, lifetimes.c.lifetime_nonce, op.source_lifetime))
        .outerjoin(result_root, _lookup(db, result_root.c.lifetime_nonce, op.result_lifetime))
        .outerjoin(counts, and_(op.kind != 'frame', _lookup(db, counts.c.result_nonce, op.result_nonce)))
        .outerjoin(bindings, _lookup(db, bindings.c.lifetime_nonce, op.source_lifetime))
        .outerjoin(namespaces, _lookup(db, namespaces.c.namespace_id, bindings.c.namespace_id))
        .outerjoin(retained, and_(op.kind == 'trim', op.phase == 'complete', _lookup(db, retained.c.version_nonce, op.source_nonce)))
    )
    return _rows(db, select(
        publications, versions.c.lifetime_nonce.label('mapped_source'), result_version.c.lifetime_nonce.label('mapped_result'),
        anchors.c.timelapse_id, anchors.c.filename, *(anchors.c[k] for k in PROVENANCE),
        result.c.timelapse_id.label('result_owner_id'), result.c.filename.label('result_filename'),
        *(result.c[k].label('result_' + k) for k in PROVENANCE),
        *(lifetimes.c['root_' + k] for k in STABLE), *(result_root.c['root_' + k].label('result_root_' + k) for k in STABLE),
        counts.c.count, bindings.c.next_number, namespaces.c.namespace_id, namespaces.c.relative_path,
        *(namespaces.c[k] for k in STABLE),
        retained.c.lifetime_nonce.label('retained_life'), retained.c.retained_by_operation, retained.c.filename.label('retained_filename'),
    ).select_from(graph).order_by(op.operation_id))


def _operation(op, rows):
    recipe_from_row(op)
    _component(op['operation_id'])
    _parts(op['source_filename'])
    _parts(op['destination_path'])
    for key in ('source_timelapse_id', 'source_printer_id', 'result_tid', 'revision'):
        number(op[key], 1)
    if op['actor_id'] is not None:
        number(op['actor_id'], 1)
    src, dst = op['source_nonce'], op['result_nonce']
    life, result_life = op['source_lifetime'], op['result_lifetime']
    if op['mapped_source'] != life or op['mapped_result'] != result_life:
        refuse()
    if (op['timelapse_id'], op['printer_id'], op['filename']) != (
        op['source_timelapse_id'], op['source_printer_id'], op['source_filename']
    ):
        refuse()
    if op['result_owner_id'] != op['result_tid'] or any(op[k] != op['result_' + k] for k in PROVENANCE):
        refuse()
    if any(op['root_' + k] != op['result_root_' + k] for k in STABLE):
        refuse()
    if op['private_directory'] != 'publish_' + op['operation_id']:
        refuse()
    _component(op['private_directory'])
    _component(op['private_name'])
    kind = op['kind']
    if kind == 'frame':
        if (dst, result_life, op['result_tid']) != (src, life, op['source_timelapse_id']):
            refuse()
        number(op['frame_number'], 1)
        number(op['payload_byte_count'], 1)
        digest(op['payload_digest_sha256'])
        if op['payload_byte_count'] > 16777216 or op['frame_number'] >= op['next_number'] or op['namespace_id'] != op['frame_namespace_id']:
            refuse()
        if op['destination_path'] != op['relative_path'] + '/frame_' + str(op['frame_number']).zfill(6) + '.jpg':
            refuse()
    else:
        if op['result_filename'] != op['destination_path'] or src == dst or op['count'] != 1:
            refuse()
        if kind in ('encode', 'trim'):
            if result_life != life or op['result_tid'] != op['source_timelapse_id']:
                refuse()
        elif kind == 'speed':
            if result_life != dst or result_life == life or op['result_tid'] == op['source_timelapse_id']:
                refuse()
        else:
            refuse()

    groups = {role: [] for role in ('root', 'source', 'source_parent', 'private_root', 'private_creation', 'output', 'output_parent')}
    directories = {}
    for row in rows:
        role, path = row['role'], row['relative_path']
        if path:
            _parts(path)
        if row['kind'] == 'directory':
            directory(row, op, private=role == 'private_root')
            identity = tuple(row[k] for k in STABLE)
            if directories.setdefault(path, identity) != identity:
                refuse()
        else:
            file_fact(row, op)
        groups[role].append(row)

    root_fact, = groups['root']
    if root_fact['relative_path'] != '' or any(root_fact[k] != op['root_' + k] for k in STABLE):
        refuse()
    sources, parents = groups['source'], groups['source_parent']
    if kind == 'frame':
        parent, = parents
        if sources or parent['relative_path'] != op['relative_path'] or any(parent[k] != op[k] for k in STABLE):
            refuse()
    else:
        if not sources or len({s['relative_path'] for s in sources}) != len(sources):
            refuse()
        if kind in ('trim', 'speed'):
            source, = sources
            if source['kind'] != 'file' or source['relative_path'] != op['source_filename']:
                refuse()
        else:
            prefix = op['relative_path'] + '/frame_'
            for source in sources:
                path = source['relative_path']
                if source['kind'] != 'file' or not path.startswith(prefix) or not path.endswith('.jpg'):
                    refuse()
                stem = path[len(prefix):-4]
                if not stem.isascii() or not stem.isdigit() or not 1 <= int(stem) < op['next_number'] or stem != str(int(stem)).zfill(6):
                    refuse()
        required = set().union(*(parent_paths(s['relative_path']) for s in sources))
        if {p['relative_path'] for p in parents} != required or len(parents) != len(required):
            refuse()
        if sum(s['byte_count'] for s in sources if s['kind'] == 'file') > 32 * 1024**3:
            refuse()

    required = parent_paths(op['destination_path']) or {''}
    parents = groups['output_parent']
    if {p['relative_path'] for p in parents} != required or len(parents) != len(required):
        refuse()
    for parent in parents:
        if not parent['relative_path'] and any(parent[k] != root_fact[k] for k in STABLE):
            refuse()
        if kind == 'frame' and any(parent[k] != op[k] for k in STABLE):
            refuse()

    private, creation, output = groups['private_root'], groups['private_creation'], groups['output']
    if op['phase'] == 'admitted':
        if private or creation or output:
            refuse()
    else:
        folder, = private
        created, = creation
        if folder['relative_path'] != op['private_directory'] or created['relative_path'] != op['private_name']:
            refuse()
        if folder['inode'] == op['root_inode'] or created['mode'] != 33152 or created['byte_count'] != 0 or created['digest_sha256'] != EMPTY:
            refuse()
        if any(created['inode'] == s['inode'] and created['device'] == s['device'] for s in sources):
            refuse()
        if op['phase'] in ('private_created', 'worker_admitted'):
            if output:
                refuse()
        else:
            produced, = output
            if produced['relative_path'] != op['private_name'] or any(produced[k] != created[k] for k in STABLE):
                refuse()
            if not 1 <= produced['byte_count'] <= 32 * 1024**3:
                refuse()
            if kind == 'frame' and (produced['byte_count'], produced['digest_sha256']) != (op['payload_byte_count'], op['payload_digest_sha256']):
                refuse()
    if op['published_ctime_ns'] is not None:
        number(op['published_ctime_ns'])
    if kind == 'trim' and op['phase'] == 'complete':
        if (op['retained_life'], op['retained_by_operation'], op['retained_filename']) != (life, op['operation_id'], op['source_filename']):
            refuse()


def _encode_sources(db):
    # _operation already proved the exact prefix, numeric range and spelling.
    # Restrict the cast to encode sources even if the planner reorders joins.
    frame = publications.alias('frame')
    output = facts.alias('output')
    source = facts.c
    op = publications.c
    prefix_length = func.length(namespaces.c.relative_path) + len('/frame_')
    stem = func.substr(source.relative_path, prefix_length + 1,
                       func.length(source.relative_path) - prefix_length - len('.jpg'))
    encoded_source = and_(op.kind == 'encode', source.role == 'source')
    number = cast(case((encoded_source, stem)), BigInteger)
    proof = (
        publications.join(facts, _lookup(db, source.operation_id, op.operation_id))
        .join(bindings, _lookup(db, bindings.c.lifetime_nonce, op.source_lifetime))
        .join(namespaces, _lookup(db, namespaces.c.namespace_id, bindings.c.namespace_id))
        .outerjoin(frame, and_(frame.c.kind == 'frame', _lookup(db, frame.c.frame_namespace_id, namespaces.c.namespace_id),
                               frame.c.frame_number == number))
        .outerjoin(output, and_(_lookup(db, output.c.operation_id, frame.c.operation_id), output.c.role == 'output'))
    )
    keys = STABLE + ('link_count', 'byte_count', 'mtime_ns', 'digest_sha256')
    query = select(facts, frame.c.phase, frame.c.payload_byte_count, frame.c.payload_digest_sha256,
                   frame.c.published_ctime_ns, output.c.operation_id.label('original'),
                   *(output.c[k].label('original_' + k) for k in keys)).select_from(proof).where(encoded_source)
    for row in _rows(db, query):
        if (row['phase'] != 'complete' or row['payload_byte_count'] != row['byte_count']
                or row['payload_digest_sha256'] != row['digest_sha256'] or row['published_ctime_ns'] != row['ctime_ns']
                or row['original'] is None or any(row[k] != row['original_' + k] for k in keys)):
            refuse()


def _retirements(db):
    current = anchors.alias('retiring_owner')
    retirement_version = versions.alias('retiring_version')
    ref = retained.c
    retired = ref.state.collate('C' if db.dialect.name == 'postgresql' else 'BINARY') == 'retired'
    # Load retirement evidence only for the exact object referenced by the trim.
    graph = (
        retained.outerjoin(publications, _lookup(db, publications.c.operation_id, ref.retained_by_operation))
        .outerjoin(versions, _lookup(db, versions.c.version_nonce, ref.version_nonce))
        .outerjoin(anchors, _lookup(db, anchors.c.operation_id, ref.version_nonce))
        .outerjoin(intent, and_(retired, _lookup(db, intent.c.operation_id, ref.retire_operation_id)))
        .outerjoin(retirement_version, _lookup(db, retirement_version.c.version_nonce, ref.retire_operation_id))
        .outerjoin(current, _lookup(db, current.c.operation_id, ref.retire_operation_id))
        .outerjoin(lifetimes, _lookup(db, lifetimes.c.lifetime_nonce, retirement_version.c.lifetime_nonce))
        .outerjoin(objects, and_(_lookup(db, objects.c.operation_id, ref.retire_operation_id), _lookup(db, objects.c.relative_path, ref.filename)))
        .outerjoin(facts, and_(_lookup(db, facts.c.operation_id, publications.c.operation_id), facts.c.role == 'source', _lookup(db, facts.c.relative_path, ref.filename)))
    )
    for receipt in (moves, unlinks):
        graph = graph.outerjoin(receipt, and_(_lookup(db, receipt.c.operation_id, ref.retire_operation_id), receipt.c.object_index == objects.c.object_index))
    statement = select(retained, publications.c.kind, publications.c.phase, publications.c.source_nonce,
                       publications.c.source_lifetime, versions.c.lifetime_nonce, anchors.c.filename, intent,
                       retirement_version.c.lifetime_nonce, current, lifetimes.c.root_device, lifetimes.c.root_inode,
                       objects, facts, moves, unlinks).select_from(graph)
    with db.execute(statement.execution_options(yield_per=512)) as result:
        for row in result.mappings():
            op = publications.c
            if row[op.kind] != 'trim' or row[op.phase] != 'complete' or (
                row[versions.c.lifetime_nonce], row[anchors.c.filename], row[op.source_nonce], row[op.source_lifetime]
            ) != (row[ref.lifetime_nonce], row[ref.filename], row[ref.version_nonce], row[ref.lifetime_nonce]):
                refuse()
            if row[ref.state] != 'retired':
                continue
            if row[objects.c.state] == 'retired':
                if (row[moves.c.phase] != 'staged' or row[moves.c.verified_ctime_ns] is None or row[unlinks.c.phase] != 'retired'
                        or row[facts.c.operation_id] is None or row[moves.c.digest_sha256] != row[facts.c.digest_sha256]
                        or any(row[objects.c[k]] != row[facts.c[k]] for k in FILE)):
                    refuse()
            if (row[intent.c.state] != 'complete' or row[retirement_version.c.lifetime_nonce] != row[ref.lifetime_nonce]
                    or any(row[intent.c[k]] != row[current.c[k]] for k in ('timelapse_id', 'printer_id', 'filename', 'owner_state', 'owner_org_id'))
                    or any(row[intent.c[k]] != row[lifetimes.c[k]] for k in ('root_device', 'root_inode'))
                    or row[objects.c.kind] != 'file' or row[objects.c.state] not in ('retired', 'originally_absent')):
                refuse()


def validate_retained(db):
    inspector = inspect(db)
    if not all(inspector.has_table(t.name) for t in (*TABLES, anchors, intent, objects, moves, unlinks)):
        # Only standalone adoption can see an incomplete schema. The pinned
        # walk opens earlier tables lazily; preserve that exact footprint rule.
        from core.timelapse_publication_validation import validate_retained as validate_partial
        return validate_partial(db)
    try:
        _authority(db)
        # Two ordered index scans bound memory to one operation's manifest.
        # Ignore orphan facts here as 019 does; its _table checks foreign keys.
        linked_facts = select(publications.c.operation_id.label('publication_key'), facts).select_from(
            publications.join(facts, _lookup(db, facts.c.operation_id, publications.c.operation_id))
        ).order_by(publications.c.operation_id)
        fact_rows = _rows(db, linked_facts)
        groups = groupby(fact_rows, itemgetter('publication_key'))
        operations = _operations(db)
        try:
            for op in operations:
                op_id, rows = next(groups)
                if op_id != op['operation_id']:
                    refuse()
                manifest = list(islice(rows, MAX_OBJECTS + 1))
                if len(manifest) > MAX_OBJECTS:
                    refuse()
                _operation(op, manifest)
        finally:
            operations.close()
            fact_rows.close()
        _encode_sources(db)
        _retirements(db)
    except MigrationError:
        raise
    except Exception:
        refuse()
