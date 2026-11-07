"""Archive TOC inventory and trusted data restore ordering."""
from __future__ import annotations

def _toc_kind(line: str) -> str | None:
    haystack = f" {line} "
    for kind in service._ALLOWED_TOC_KINDS:
        if f" {kind} " in haystack:
            return kind
    return None


def _toc_inventory(toc: str) -> service.PostgreSQLTOCInventory:
    structure: set[str] = set()
    table_data: set[str] = set()
    sequence_sets: set[str] = set()
    entries = 0
    descriptors: set[str] = set()
    for raw_line in toc.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(";"):
            continue
        entries += 1
        if " SCHEMA - public " in f" {line} ":
            continue
        match = service.re.match(r"^\d+;\s+\d+\s+\d+\s+(.+)$", line)
        if not match:
            raise service.BackupValidationError("PostgreSQL archive TOC entry is malformed")
        body = match.group(1)
        descriptor, separator, _owner = body.rpartition(" ")
        if not separator or not descriptor:
            raise service.BackupValidationError("PostgreSQL archive TOC entry is malformed")
        if descriptor in descriptors:
            raise service.BackupValidationError('PostgreSQL archive contains duplicate object entries')
        descriptors.add(descriptor)
        kind = service._toc_kind(descriptor)
        if kind == "TABLE DATA":
            table_data.add(descriptor)
        elif kind == "SEQUENCE SET":
            sequence_sets.add(descriptor)
        else:
            structure.add(descriptor)
    return service.PostgreSQLTOCInventory(
        structure=frozenset(structure),
        table_data=frozenset(table_data),
        sequence_sets=frozenset(sequence_sets),
        entries=entries,
    )


def validate_archive_toc(
    toc: str,
    *,
    allowed_structure: frozenset[str] | None = None,
    allow_unresolved_structure: bool = False,
    _contract=None,
) -> dict[str, object]:
    """Reject archive objects outside the canonical ODIN public-schema set."""
    tables = (service.canonical_tables() if _contract is None else service.canonical_tables(_contract=_contract))
    archive_tables: set[str] = set()
    for raw_line in toc.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(";"):
            continue
        if " SCHEMA - public " in f" {line} ":
            continue
        upper = f" {line.upper()} "
        if any(f" {kind} " in upper for kind in service._FORBIDDEN_TOC_KINDS):
            raise service.BackupValidationError("PostgreSQL archive contains a forbidden object type")
        kind = service._toc_kind(line)
        if not kind:
            raise service.BackupValidationError("PostgreSQL archive contains an unsupported object type")
        if " public " not in f" {line} ":
            raise service.BackupValidationError("PostgreSQL archive contains an object outside public schema")
        if kind in {"TABLE", "TABLE DATA"}:
            match = service.re.search(rf" {service.re.escape(kind)} public ([^ ]+) ", f" {line} ")
            if not match or match.group(1) not in tables:
                raise service.BackupValidationError("PostgreSQL archive contains a non-ODIN table")
            if kind == "TABLE":
                archive_tables.add(match.group(1))
        elif kind == "TYPE":
            match = service.re.search(r" TYPE public ([^ ]+) ", f" {line} ")
            if not match or match.group(1) not in service.canonical_enum_types():
                raise service.BackupValidationError("PostgreSQL archive contains a non-ODIN type")
        elif kind == "FUNCTION":
            from modules.system.postgres_canonical_executables import canonical_functions
            match = service.re.search(r" FUNCTION public ([^ ]+) ", f" {line} ")
            if not match or match.group(1) not in {name+'()' for name in (canonical_functions() if _contract is None else canonical_functions(_contract=_contract))}:
                raise service.BackupValidationError("PostgreSQL archive contains a forbidden object type")
        elif kind == "TRIGGER":
            from modules.system.postgres_canonical_executables import canonical_triggers
            match = service.re.search(r" TRIGGER public ([^ ]+) ([^ ]+) ", f" {line} ")
            if not match or tuple(match.groups()) not in (canonical_triggers() if _contract is None else canonical_triggers(_contract=_contract)):
                raise service.BackupValidationError("PostgreSQL archive contains a forbidden object type")
        elif kind in {"DEFAULT", "CONSTRAINT", "FK CONSTRAINT"}:
            match = service.re.search(rf" {service.re.escape(kind)} public ([^ ]+) ", f" {line} ")
            if not match or match.group(1) not in tables:
                raise service.BackupValidationError(
                    "PostgreSQL archive contains a non-ODIN structural object"
                )
    missing = sorted(tables - archive_tables)
    if missing:
        raise service.BackupValidationError(
            "PostgreSQL archive is missing required ODIN tables: " + ", ".join(missing)
        )
    inventory = service._toc_inventory(toc)
    unresolved = {
        descriptor
        for descriptor in inventory.structure
        if not descriptor.startswith("TABLE public ")
        and not descriptor.startswith("TYPE public ")
    }
    if allowed_structure is None:
        if (unresolved or inventory.sequence_sets) and not allow_unresolved_structure:
            raise service.BackupValidationError(
                "PostgreSQL archive contains a non-ODIN structural object"
            )
    elif {entry for entry in inventory.structure if not entry.startswith(('FUNCTION public ', 'TYPE public '))} != {entry for entry in allowed_structure if not entry.startswith(('FUNCTION public ', 'TYPE public '))}:
        missing_structure = sorted(allowed_structure - inventory.structure)[:12]
        extra_structure = sorted(inventory.structure - allowed_structure)[:12]
        raise service.BackupValidationError(
            "PostgreSQL archive structural objects do not match the canonical ODIN schema; "
            f"missing={missing_structure}; extra={extra_structure}"
        )

    if allowed_structure is not None:
        expected_table_data = frozenset(
            f"TABLE DATA public {table_name}" for table_name in tables
        )
        expected_sequence_sets = frozenset(
            descriptor.replace("SEQUENCE public ", "SEQUENCE SET public ", 1)
            for descriptor in allowed_structure
            if descriptor.startswith("SEQUENCE public ")
        )
        if inventory.table_data != expected_table_data:
            raise service.BackupValidationError(
                "PostgreSQL archive table-data objects do not match the canonical ODIN schema"
            )
        if inventory.sequence_sets != expected_sequence_sets:
            raise service.BackupValidationError(
                "PostgreSQL archive sequence-set objects do not match the canonical ODIN schema"
            )
    return {
        "toc_entries": inventory.entries,
        "table_count": len(archive_tables),
        "toc_fingerprint": service.hashlib.sha256(
            "\n".join(
                sorted(
                    inventory.structure
                    | inventory.table_data
                    | inventory.sequence_sets
                )
            ).encode("utf-8")
        ).hexdigest(),
    }


def _ordered_restore_data(lines, expected_tables, foreign_keys):
    """Retain verified TOC records; order COPY by trusted target constraints."""
    tables={};sequences=[]
    for line in lines:
        match=service.re.fullmatch(r'\d+;\s+\d+\s+\d+\s+(TABLE DATA|SEQUENCE SET) public ([^ ]+) [^ ]+',line.strip())
        if not match:raise service.BackupValidationError('Restore data TOC entry is invalid')
        kind,name=match.groups()
        if kind=='SEQUENCE SET':sequences.append(line);continue
        if name not in expected_tables or name in tables:
            raise service.BackupValidationError('Restore table data is foreign or duplicated')
        tables[name]=line
    if set(tables)!=set(expected_tables):raise service.BackupValidationError('Restore table data is missing')
    parents={name:set() for name in tables}
    for child,parent,deferrable,deferred in foreign_keys:
        if child not in tables or parent not in tables:
            raise service.BackupValidationError('Restore foreign key parent is missing or foreign')
        if child==parent or (deferrable and deferred):continue
        parents[child].add(parent)
    ordered=[]
    while parents:
        ready=sorted(name for name,dependencies in parents.items() if not dependencies)
        if not ready:raise service.BackupValidationError('Restore immediate foreign key cycle is unsupported')
        # One lexical choice per iteration gives deterministic table-name ties.
        name=ready[0];ordered.append(tables[name]);del parents[name]
        for dependencies in parents.values():dependencies.discard(name)
    return ordered+sequences


def _trusted_restore_foreign_keys(connection):
    return connection.execute(service.text("""SELECT child.relname,CASE WHEN pn.nspname='public' THEN parent.relname ELSE pn.nspname||'.'||parent.relname END,k.condeferrable,k.condeferred
        FROM pg_constraint k JOIN pg_class child ON child.oid=k.conrelid
        JOIN pg_namespace cn ON cn.oid=child.relnamespace
        JOIN pg_class parent ON parent.oid=k.confrelid
        JOIN pg_namespace pn ON pn.oid=parent.relnamespace
        WHERE k.contype='f' AND cn.nspname='public'
        ORDER BY child.relname,parent.relname,k.conname""")).all()


def _validated_restore_list(
    archive: service.Path,
    database_url: str,
    password_file: str | None,
    directory: service.Path,
    *,
    allowed_structure: frozenset[str] | None = None,
    trusted_prevalidated: bool = False,
    phase: str = 'data',
    schema_descriptors: frozenset[str] | None = None,
    data_connection=None,
    _contract=None,
) -> service.Path:
    toc = service._run_pg_tool(
        ["pg_restore", "--list", str(archive)], database_url, password_file, directory
    )
    service.validate_archive_toc(
        toc,
        allowed_structure=allowed_structure,
        allow_unresolved_structure=trusted_prevalidated,
        _contract=_contract,
    )
    if phase=='data' and data_connection is None:
        raise service.BackupValidationError('Restore data requires validated canonical target metadata')
    selected=[]
    descriptor, filename = service.tempfile.mkstemp(prefix=".odin-restore-list-", dir=directory)
    path = service.Path(filename)
    try:
        with service.os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            if phase not in ('data', 'schema-evidence'):
                raise service.BackupValidationError('Unsupported restore phase')
            for line in toc.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith(';'):
                    continue
                if " SCHEMA - public " in f" {line} ":
                    continue
                match = service.re.match(r'^\d+;\s+\d+\s+\d+\s+(.+)$', stripped)
                if not match:
                    raise service.BackupValidationError('PostgreSQL archive TOC entry is malformed')
                descriptor = match.group(1).rpartition(' ')[0]
                kind = service._toc_kind(descriptor)
                if phase == 'data':
                    if kind not in ('TABLE DATA', 'SEQUENCE SET'):
                        continue
                elif kind in ('TABLE DATA', 'SEQUENCE SET') or (schema_descriptors is not None and descriptor not in schema_descriptors):
                    continue
                selected.append(line)
            if phase=='data':
                selected=service._ordered_restore_data(selected,(service.canonical_tables() if _contract is None else service.canonical_tables(_contract=_contract)),service._trusted_restore_foreign_keys(data_connection))
            handle.write(''.join(line+'\n' for line in selected))
            handle.flush()
            service.os.fsync(handle.fileno())
        path.chmod(0o600)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def inspect_postgres_archive(
    archive: service.Path,
    database_url: str,
    *,
    password_file: str | None = None,
    work_dir: service.Path | None = None,
    allowed_structure: frozenset[str] | None = None,
    trusted_prevalidated: bool = False,
    _contract=None,
) -> dict[str, object]:
    if not archive.is_file():
        raise service.BackupValidationError("PostgreSQL backup file does not exist")
    size = archive.stat().st_size
    if size < 100 or size > service.MAX_BACKUP_BYTES:
        raise service.BackupValidationError("PostgreSQL backup size is outside the supported range")
    directory = work_dir or archive.parent
    toc = service._run_pg_tool(
        ["pg_restore", "--list", str(archive)], database_url, password_file, directory
    )
    metadata = service.validate_archive_toc(
        toc,
        allowed_structure=allowed_structure,
        allow_unresolved_structure=trusted_prevalidated,
        _contract=_contract,
    )
    return {"size_bytes": size, "sha256": service._sha256(archive), **metadata}



# Bind after definitions so either the service or this module can be imported first.
from modules.system import postgres_backup_service as service
