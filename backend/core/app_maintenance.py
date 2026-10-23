"""Application discovery and periodic database maintenance."""
import asyncio
import importlib
import logging
import pathlib
from datetime import datetime, timedelta, timezone
from sqlalchemy import text
log = logging.getLogger("odin.api")

def _discover_modules() -> list[str]:
    """Return a list of module package names found under backend/modules/.

    A valid module directory contains an __init__.py with a MODULE_ID attribute.
    """
    modules_dir = pathlib.Path(__file__).parent.parent / "modules"
    found = []
    for entry in sorted(modules_dir.iterdir()):
        if entry.name.startswith("_") or not entry.name.isidentifier() or not entry.is_dir():
            continue
        init_file = entry / "__init__.py"
        if not init_file.exists():
            continue
        pkg_name = f"modules.{entry.name}"
        try:
            mod = importlib.import_module(pkg_name)  # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import -- verified safe — plugin discovery via os.scandir, not user input
            if hasattr(mod, "MODULE_ID"):
                found.append(pkg_name)
        except Exception as exc:
            log.warning(f"Module discovery: skipping {pkg_name!r} — {exc}")
    return found


def _resolve_load_order(pkg_names: list[str]) -> list[str]:
    """Topologically sort modules so that providers load before their consumers.

    Algorithm:
    1. Build a map of interface_name -> module_pkg for IMPLEMENTS declarations.
    2. For each module's REQUIRES list, find which module pkg provides that interface.
    3. Perform Kahn's topological sort (no-dependency modules first).
    4. Any modules with circular or unresolvable deps load in discovery order at
       the end (with a warning) rather than crashing startup.
    """
    # Load manifest metadata
    mods: dict[str, dict] = {}
    for pkg in pkg_names:
        m = importlib.import_module(pkg)  # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import -- verified safe — plugin discovery via os.scandir, not user input
        mods[pkg] = {
            "implements": getattr(m, "IMPLEMENTS", []),
            "requires": getattr(m, "REQUIRES", []),
        }

    # interface -> providing pkg
    providers: dict[str, str] = {}
    for pkg, info in mods.items():
        for iface in info["implements"]:
            providers[iface] = pkg

    # Build dependency edges: pkg -> set of pkgs it depends on
    edges: dict[str, set[str]] = {pkg: set() for pkg in pkg_names}
    for pkg, info in mods.items():
        for iface in info["requires"]:
            provider_pkg = providers.get(iface)
            if provider_pkg and provider_pkg != pkg:
                edges[pkg].add(provider_pkg)

    # Kahn's algorithm — in_degree = number of dependencies each pkg has
    in_degree: dict[str, int] = {pkg: len(deps) for pkg, deps in edges.items()}

    queue = sorted(pkg for pkg in pkg_names if in_degree[pkg] == 0)
    ordered: list[str] = []

    while queue:
        pkg = queue.pop(0)
        ordered.append(pkg)
        # Find all packages that depend on pkg and decrement their in_degree
        for other_pkg, deps in edges.items():
            if pkg in deps:
                in_degree[other_pkg] -= 1
                if in_degree[other_pkg] == 0:
                    queue.append(other_pkg)
                    queue.sort()

    # Any remaining packages have circular deps — append in discovery order
    remaining = [pkg for pkg in pkg_names if pkg not in ordered]
    if remaining:
        log.warning(
            f"Module load order: circular or unresolvable dependencies for "
            f"{remaining} — appending in discovery order."
        )
        ordered.extend(remaining)

    return ordered


async def _periodic_cleanup():
    """Background task: hourly cleanup of stale login attempts, sessions, and expired tokens."""
    from core.db import SessionLocal

    while True:
        await asyncio.sleep(3600)
        try:
            db = SessionLocal()
            try:
                now = datetime.now(timezone.utc)
                db.execute(
                    text("DELETE FROM login_attempts WHERE attempted_at < :cutoff"),
                    {"cutoff": (now - timedelta(minutes=30)).timestamp()},
                )
                db.execute(
                    text("DELETE FROM active_sessions WHERE created_at < :cutoff"),
                    {"cutoff": (now - timedelta(hours=48)).isoformat()},
                )
                db.execute(
                    text("DELETE FROM token_blacklist WHERE expires_at < :now"),
                    {"now": now.isoformat()},
                )
                # v1.8.6 (codex pass 4): the digest-sends idempotency tables
                # grow monotonically without this — one row per user per
                # quiet-hours window forever. 30-day retention is well past
                # any reasonable troubleshooting window for "did I get the
                # 2026-Q1-04-13 digest?" while keeping the row count bounded.
                # Tables are guarded with `IF EXISTS` since older
                # deployments may not have run migrations 002/003 yet.
                # Inlined to avoid a semgrep false-positive on f-string
                # interpolation into text() — the alternative was a
                # hardcoded-allowlist loop, which semgrep flags anyway.
                # Wrapped each in its own try so a missing table on an
                # older deployment doesn't kill the other delete.
                _digest_cutoff = (now - timedelta(days=30)).isoformat()
                try:
                    db.execute(
                        text("DELETE FROM quiet_hours_digest_sends WHERE window_ended_at < :cutoff"),
                        {"cutoff": _digest_cutoff},
                    )
                except Exception:
                    db.rollback()
                try:
                    db.execute(
                        text("DELETE FROM quiet_hours_org_digest_sends WHERE window_ended_at < :cutoff"),
                        {"cutoff": _digest_cutoff},
                    )
                except Exception:
                    db.rollback()
                db.commit()

                # v1.8.9: prune expired idempotency-cache rows. 24h TTL
                # is enforced at read time too, but the hourly prune
                # keeps the table from growing unbounded. Wrapped in its
                # own try so a missing table on an older deployment
                # (migration 005 not applied) doesn't block the rest.
                try:
                    from core.middleware.idempotency import prune_expired_idempotency_keys
                    pruned = prune_expired_idempotency_keys(db)
                    if pruned:
                        log.info("Pruned %d expired idempotency-key rows", pruned)
                except Exception:
                    db.rollback()
                    log.debug("idempotency-key prune skipped (table may not exist yet)")

                log.info("Periodic cleanup completed: stale sessions, login attempts, expired tokens, old digest-send rows, idempotency cache")
            finally:
                db.close()
        except Exception:
            log.warning("Periodic cleanup failed", exc_info=True)


# ---------------------------------------------------------------------------
# Schema drift check (unchanged from original main.py)
# ---------------------------------------------------------------------------

_DUAL_SCHEMA_TABLES = [
    "vision_detections", "vision_settings", "vision_models",
    "timelapses", "nozzle_lifecycle",
    "consumables", "product_consumables", "consumable_usage",
]


def _check_schema_drift(engine, Base):
    """Compare SQLAlchemy column definitions against dialect-neutral inspection."""
    try:
        from sqlalchemy import inspect

        with engine.connect() as conn:
            inspector = inspect(conn)
            for table_name in _DUAL_SCHEMA_TABLES:
                sa_table = Base.metadata.tables.get(table_name)
                if sa_table is None:
                    continue
                sa_cols = {c.name for c in sa_table.columns}
                if table_name not in inspector.get_table_names():
                    continue
                db_cols = {
                    column["name"] for column in inspector.get_columns(table_name)
                }
                only_sa = sa_cols - db_cols
                only_db = db_cols - sa_cols
                if only_sa:
                    log.warning(
                        f"Schema drift [{table_name}]: columns in models.py "
                        f"but not in DB: {sorted(only_sa)}"
                    )
                if only_db:
                    log.warning(
                        f"Schema drift [{table_name}]: columns in DB "
                        f"but not in models.py: {sorted(only_db)}"
                    )
    except Exception:
        log.debug("Schema drift check skipped", exc_info=True)


def _revoke_legacy_biometric_tokens(engine) -> None:
    """Delete all biometric refresh tokens once, on first start of this version.

    v1.9.24 and earlier let a scoped API token mint a biometric refresh token
    (SEC HIGH-1). Any such token already issued keeps working until it expires,
    so on the first start after upgrade every biometric token is revoked and
    users re-enrol through an interactive session. A system_config marker makes
    it run exactly once; it is not a schema change.
    """
    from sqlalchemy import text
    from core.db_compat import sql
    marker = "biometric_revoked_2_0"
    with engine.begin() as connection:
        done = connection.execute(
            text("SELECT 1 FROM system_config WHERE key = :k"), {"k": marker}
        ).first()
        if done:
            return
        deleted = connection.execute(text("DELETE FROM biometric_tokens")).rowcount
        connection.execute(
            text(f"{sql.insert_or_ignore_prefix()} system_config (key, value) "  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fragments from core.db_compat sql.* dialect helpers called with literal arguments (closed per-dialect output); values are bound
                 f"VALUES (:k, :v){sql.on_conflict_ignore('key')}"),
            {"k": marker, "v": "true"},
        )
        if deleted:
            log.warning("Revoked %d pre-2.0 biometric refresh token(s); affected devices must re-enrol.", deleted)


