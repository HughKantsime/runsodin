#!/usr/bin/env python3
"""Full demo wipe/seed; run only on the demo installation.

Copy both seed_demo_full.py and seed_demo_data.py into the container, then run
with PYTHONPATH=/app/backend so the installed core.job_identity allocator is available.
"""
import json
import random
import sqlite3
import string
import sys
from pathlib import Path
from datetime import timedelta
from seed_demo_data import (
    DB_PATH, LICENSE_OUTPUT, NOW, TODAY, iso,
    rand_dt, bcrypt_hash, generate_license, USERS, PRINTERS,
    STATES_POOL, FILAMENT_COLORS, FILAMENT_TYPES, FILAMENT_LIBRARY, MODELS,
    PRODUCTS, CUSTOMER_FIRST, CUSTOMER_LAST, CONSUMABLES, HMS_ERRORS,
    MAINTENANCE_TASKS, ALERT_TEMPLATES, AUDIT_ACTIONS, AUDIT_ENTITIES,
)


def main():
    print("=" * 60)
    print("O.D.I.N. Demo Seed — Full Wipe & Replace")
    print("=" * 60)

    # Support the documented container copy at /seed_demo_full.py and repo use.
    backend = Path(__file__).resolve().parent.parent / "backend"
    sys.path.insert(0, str(backend if backend.is_dir() else Path("/app/backend")))
    from core.job_identity import insert_job
    from core.audit_integrity import install_sqlite
    conn = sqlite3.connect(DB_PATH)
    install_sqlite(conn)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=OFF")

    # ── Wipe all tables ──
    print("\n[1/14] Wiping existing data...")
    tables = [
        "controlled_files", "controlled_jobs", "controlled_job_tombstones",
        "controlled_retention_clocks", "controlled_audience_members", "controlled_audiences",
        "alerts", "alert_preferences", "push_subscriptions",
        "audit_logs", "spool_usage", "consumable_usage",
        "product_consumables", "order_items", "orders",
        "product_components", "products", "consumables",
        "maintenance_logs", "maintenance_tasks",
        "nozzle_lifecycle", "hms_error_history", "printer_telemetry",
        "ams_telemetry", "print_jobs", "print_files",
        "jobs", "filament_slots", "spools", "filament_library",
        "models", "scheduler_runs", "system_config",
        "printers", "users", "webhooks", "oidc_config",
    ]
    for t in tables:
        try:
            conn.execute(f"DELETE FROM {t}")
        except sqlite3.OperationalError:
            pass  # table may not exist
    conn.commit()
    # Reset autoincrement counters
    try:
        conn.execute("DELETE FROM sqlite_sequence")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    print("  ✓ All tables wiped")

    # ── Users ──
    print("\n[2/14] Seeding users...")
    for username, password, email, role in USERS:
        pw_hash = bcrypt_hash(password)
        conn.execute(
            "INSERT INTO users (username, password_hash, email, role, is_active, created_at) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (username, pw_hash, email, role, iso(rand_dt(90, 30)))
        )
    conn.commit()
    print(f"  ✓ {len(USERS)} users")

    # ── Printers ──
    print("\n[3/14] Seeding printers...")
    printer_ids = []
    for i, (name, nick, model, api_type, slots, ip_suffix) in enumerate(PRINTERS):
        state = STATES_POOL[i]
        total_hours = random.uniform(50, 2000)
        total_prints = int(total_hours / random.uniform(1.5, 3.0))
        bed_temp = random.uniform(55, 60) if state == "RUNNING" else random.uniform(22, 28)
        nozzle_temp = random.uniform(200, 260) if state == "RUNNING" else random.uniform(22, 28)
        fan_speed = random.randint(50, 100) if state == "RUNNING" else 0
        last_seen = iso(NOW - timedelta(minutes=random.randint(0, 5))) if state != "OFFLINE" else iso(rand_dt(7, 1))

        conn.execute("""
            INSERT INTO printers (
                name, nickname, model, api_type, api_host, slot_count,
                is_active, gcode_state, bed_temp, nozzle_temp,
                bed_target_temp, nozzle_target_temp, fan_speed,
                total_print_hours, total_print_count,
                hours_since_maintenance, prints_since_maintenance,
                last_seen, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            name, nick, model, api_type, f"192.168.1.{ip_suffix}", slots,
            1 if state != "OFFLINE" else 0, state,
            round(bed_temp, 1), round(nozzle_temp, 1),
            60.0 if state == "RUNNING" else 0,
            215.0 if state == "RUNNING" else 0,
            fan_speed,
            round(total_hours, 1), total_prints,
            round(random.uniform(10, 200), 1), random.randint(5, 80),
            last_seen,
            iso(rand_dt(180, 90)), iso(NOW),
        ))
        printer_ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()
    print(f"  ✓ {len(PRINTERS)} printers")

    # ── Filament Library ──
    print("\n[4/14] Seeding filament library & spools...")
    fl_ids = []
    for brand, name, material, color_hex, cost_per_g in FILAMENT_LIBRARY:
        conn.execute(
            "INSERT INTO filament_library (brand, name, material, color_hex, cost_per_gram, is_custom, created_at) "
            "VALUES (?, ?, ?, ?, ?, 0, ?)",
            (brand, name, material, color_hex, cost_per_g, iso(rand_dt(180, 60)))
        )
        fl_ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()
    print(f"  ✓ {len(FILAMENT_LIBRARY)} filament library entries")

    # ── Spools ──
    spool_ids = []
    color_names = list(FILAMENT_COLORS.keys())
    for i in range(25):
        fl = random.choice(fl_ids)
        remaining = random.choice([random.uniform(5, 80)] * 4 + [random.uniform(100, 950)])  # some nearly empty
        initial = 1000.0
        color_hex = FILAMENT_LIBRARY[fl_ids.index(fl)][3]
        status = "ACTIVE" if remaining > 10 else "EMPTY"
        conn.execute("""
            INSERT INTO spools (
                filament_id, initial_weight_g, remaining_weight_g, spool_weight_g,
                color_hex, price, purchase_date, vendor, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            fl, initial, round(remaining, 1), 250.0,
            color_hex, round(random.uniform(15, 35), 2),
            iso(rand_dt(120, 10)), random.choice(["Bambu Lab", "Hatchbox", "eSUN", "Polymaker"]),
            status, iso(rand_dt(120, 10)), iso(NOW),
        ))
        spool_ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()
    print(f"  ✓ 25 spools")

    # ── Filament Slots ──
    print("\n[5/14] Seeding filament slots...")
    slot_count = 0
    # Realistic AMS loadouts: common color combos per printer
    AMS_LOADOUTS = [
        # (color, hex, type) — common 4-slot AMS configurations
        [("Black", "#1a1a1a", "PLA"), ("White", "#f5f5f5", "PLA"), ("Red", "#dc2626", "PLA"), ("Blue", "#2563eb", "PLA")],
        [("Black", "#1a1a1a", "PLA"), ("White", "#f5f5f5", "PLA"), ("Green", "#16a34a", "PLA"), ("Orange", "#ea580c", "PLA")],
        [("Black", "#1a1a1a", "PETG"), ("White", "#f5f5f5", "PETG"), ("Blue", "#2563eb", "PETG"), ("Grey", "#6b7280", "PETG")],
        [("Grey", "#6b7280", "PLA"), ("White", "#f5f5f5", "PLA"), ("Gold", "#d4a017", "PLA"), ("Black", "#1a1a1a", "PLA")],
        [("Black", "#1a1a1a", "PLA"), ("Red", "#dc2626", "PLA"), ("Yellow", "#eab308", "PLA"), ("Blue", "#2563eb", "PLA")],
    ]
    # Single-slot common colors
    SINGLE_COLORS = [
        ("Black", "#1a1a1a", "PLA"), ("White", "#f5f5f5", "PLA"), ("Grey", "#6b7280", "PLA"),
        ("Black", "#1a1a1a", "PETG"), ("White", "#f5f5f5", "PETG"), ("Red", "#dc2626", "PLA"),
        ("Blue", "#2563eb", "PLA"), ("Orange", "#ea580c", "PLA"), ("Black", "#1a1a1a", "ABS"),
        ("Black", "#1a1a1a", "TPU"),
    ]
    for pid_idx, (name, nick, model, api_type, slots, ip_suffix) in enumerate(PRINTERS):
        pid = printer_ids[pid_idx]
        if slots >= 4:
            loadout = AMS_LOADOUTS[pid_idx % len(AMS_LOADOUTS)]
            for s in range(1, slots + 1):
                color_name, color_hex, ftype = loadout[(s-1) % len(loadout)]
                conn.execute("""
                    INSERT INTO filament_slots (
                        printer_id, slot_number, filament_type, color, color_hex, loaded_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                """, (pid, s, ftype, color_name, color_hex, iso(rand_dt(30, 0))))
                slot_count += 1
        else:
            for s in range(1, slots + 1):
                color_name, color_hex, ftype = SINGLE_COLORS[pid_idx % len(SINGLE_COLORS)]
                conn.execute("""
                    INSERT INTO filament_slots (
                        printer_id, slot_number, filament_type, color, color_hex, loaded_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                """, (pid, s, ftype, color_name, color_hex, iso(rand_dt(30, 0))))
                slot_count += 1
    conn.commit()
    print(f"  ✓ {slot_count} filament slots")

    # ── Models ──
    print("\n[6/14] Seeding models...")
    model_ids = []
    for name, build_time, ftype, cost, markup, units_bed, category in MODELS:
        n_colors = random.randint(1, 2)
        colors_req = json.dumps({
            f"slot_{j+1}": {"color": random.choice(list(FILAMENT_COLORS.keys())), "grams": round(random.uniform(5, 50), 1)}
            for j in range(n_colors)
        })
        conn.execute("""
            INSERT INTO models (
                name, build_time_hours, default_filament_type, color_requirements,
                category, cost_per_item, units_per_bed, quantity_per_bed,
                markup_percent, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            name, build_time, ftype, colors_req,
            category, cost, units_bed, units_bed,
            markup, iso(rand_dt(120, 30)), iso(NOW),
        ))
        model_ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()
    print(f"  ✓ {len(MODELS)} models")

    # ── Products & Components ──
    print("\n[7/14] Seeding products & orders...")
    product_ids = []
    for name, sku, price, desc, component_indices in PRODUCTS:
        conn.execute(
            "INSERT INTO products (name, sku, price, description, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, sku, price, desc, iso(rand_dt(90, 20)), iso(NOW))
        )
        prod_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        product_ids.append(prod_id)
        for mi in component_indices:
            conn.execute(
                "INSERT INTO product_components (product_id, model_id, quantity_needed) VALUES (?, ?, ?)",
                (prod_id, model_ids[mi], 1)
            )
    conn.commit()
    print(f"  ✓ {len(PRODUCTS)} products")

    # ── Orders & Order Items ──
    ORDER_STATUSES = (["FULFILLED"] * 30 + ["IN_PROGRESS"] * 8 + ["SHIPPED"] * 5 +
                      ["PENDING"] * 4 + ["CANCELLED"] * 3)
    random.shuffle(ORDER_STATUSES)
    order_ids = []
    total_items = 0
    platforms = ["website", "etsy", "ebay", "amazon", "local", "wholesale"]
    for i in range(50):
        status = ORDER_STATUSES[i]
        order_date = rand_dt(60, 1 if status != "PENDING" else 0)
        shipped_date = iso(order_date + timedelta(days=random.randint(1, 5))) if status == "SHIPPED" else None
        customer = f"{random.choice(CUSTOMER_FIRST)} {random.choice(CUSTOMER_LAST)}"
        email = f"{customer.split()[0].lower()}.{customer.split()[1].lower()}@{'gmail.com' if random.random() > 0.3 else 'outlook.com'}"
        revenue = round(random.uniform(5, 200), 2)
        platform = random.choice(platforms)
        platform_fees = round(revenue * random.uniform(0.05, 0.15), 2) if platform != "local" else 0
        shipping_cost = round(random.uniform(3, 12), 2) if status != "CANCELLED" else 0
        shipping_charged = round(shipping_cost + random.uniform(0, 5), 2)
        tracking = f"1Z{''.join(random.choices(string.ascii_uppercase + string.digits, k=16))}" if status == "SHIPPED" else None

        conn.execute("""
            INSERT INTO orders (
                order_number, platform, customer_name, customer_email, status,
                revenue, platform_fees, payment_fees, shipping_charged, shipping_cost,
                labor_minutes, order_date, shipped_date, tracking_number,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            f"ORD-{1001+i}", platform, customer, email, status,
            revenue, platform_fees, round(revenue * 0.029 + 0.30, 2),
            shipping_charged, shipping_cost,
            random.randint(5, 45), iso(order_date), shipped_date, tracking,
            iso(order_date), iso(NOW),
        ))
        oid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        order_ids.append(oid)

        # 2-3 items per order
        n_items = random.randint(1, 4)
        for _ in range(n_items):
            prod = random.choice(product_ids)
            prod_idx = product_ids.index(prod)
            qty = random.randint(1, 5)
            unit_price = PRODUCTS[prod_idx][2]
            fulfilled = qty if status in ("FULFILLED", "SHIPPED") else random.randint(0, qty)
            conn.execute(
                "INSERT INTO order_items (order_id, product_id, quantity, unit_price, fulfilled_quantity, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (oid, prod, qty, unit_price, fulfilled, iso(order_date))
            )
            total_items += 1
    conn.commit()
    print(f"  ✓ 50 orders, {total_items} order items")

    # ── Jobs (500) ──
    print("\n[8/14] Seeding 500 jobs...")
    JOB_STATUSES = (["COMPLETED"] * 390 + ["PENDING"] * 40 + ["SCHEDULED"] * 12 +
                    ["PRINTING"] * 25 + ["FAILED"] * 20 + ["CANCELLED"] * 13)
    random.shuffle(JOB_STATUSES)
    job_ids = []
    all_color_names = list(FILAMENT_COLORS.keys())
    submitters = ["demo", "sarah", "mike"]
    order_item_ids = list(range(1, total_items + 1))
    random.shuffle(order_item_ids)
    oi_cursor = 0
    sched_counter = 0  # track which printer gets the next scheduled job

    for i in range(500):
        status = JOB_STATUSES[i]
        mid = random.choice(model_ids)
        mid_idx = model_ids.index(mid)
        m = MODELS[mid_idx]
        pid = random.choice(printer_ids)
        color = random.choice(all_color_names)
        qty = random.randint(1, m[5])  # up to units_per_bed
        build_time = m[1] * (qty / max(m[5], 1))
        est_cost = round(m[3] * qty, 2)
        sugg_price = round(est_cost * (1 + m[4] / 100), 2)

        # Timestamps based on status
        if status == "COMPLETED":
            actual_end = rand_dt(90, 1)
            actual_start = actual_end - timedelta(hours=build_time * random.uniform(0.9, 1.3))
            duration = round((actual_end - actual_start).total_seconds() / 3600, 2)
            sched_start = None
            sched_end = None
        elif status == "PRINTING":
            actual_start = NOW - timedelta(hours=build_time * random.uniform(0.1, 0.7))
            actual_end = None
            duration = None
            sched_start = None
            sched_end = None
        elif status == "SCHEDULED":
            # Each scheduled job on a different printer, evenly spaced in time
            pid = printer_ids[sched_counter % len(printer_ids)]
            base_offset = 4 + (sched_counter * 3)  # 4h, 7h, 10h, 13h, ...
            sched_start = NOW + timedelta(hours=base_offset + random.uniform(-0.5, 0.5))
            sched_end = sched_start + timedelta(hours=build_time * random.uniform(0.9, 1.1))
            actual_start = None
            actual_end = None
            duration = None
            sched_counter += 1
        elif status == "FAILED":
            actual_start = rand_dt(60, 1)
            actual_end = actual_start + timedelta(hours=build_time * random.uniform(0.1, 0.8))
            duration = round((actual_end - actual_start).total_seconds() / 3600, 2)
            sched_start = None
            sched_end = None
        else:  # PENDING, CANCELLED
            actual_start = None
            actual_end = None
            duration = None
            sched_start = None
            sched_end = None

        # Link some jobs to order items
        oi_id = None
        if status in ("COMPLETED", "PRINTING", "SCHEDULED") and oi_cursor < len(order_item_ids) and random.random() > 0.6:
            oi_id = order_item_ids[oi_cursor]
            oi_cursor += 1

        fail_reason = random.choice(["Spaghetti detected", "Filament runout", "Layer shift", "Nozzle clog", "Bed adhesion failure"]) if status == "FAILED" else None

        insert_job(conn, """
            INSERT INTO jobs (
                model_id, item_name, quantity, status, priority, printer_id,
                scheduled_start, scheduled_end, actual_start, actual_end,
                duration_hours, colors_required, filament_type, match_score,
                is_locked, hold,
                estimated_cost, suggested_price, order_item_id, quantity_on_bed,
                submitted_by, fail_reason, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            mid, m[0], qty, status, random.randint(1, 5), pid,
            iso(sched_start) if sched_start else None,
            iso(sched_end) if sched_end else None,
            iso(actual_start) if actual_start else None,
            iso(actual_end) if actual_end else None,
            duration, json.dumps([color]), m[2],
            random.randint(60, 100),
            0, 0,
            est_cost, sugg_price, oi_id, qty,
            random.choice(submitters), fail_reason,
            iso(actual_start or sched_start or rand_dt(90, 0)), iso(NOW),
        ))
        job_ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    conn.commit()
    print(f"  ✓ 500 jobs")

    # ── Print Jobs (200) — historical MQTT-tracked ──
    print("\n[9/14] Seeding print_jobs and telemetry...")
    bambu_pids = [printer_ids[i] for i, p in enumerate(PRINTERS) if p[3] == "bambu"]
    PJ_STATUSES = ["completed"] * 160 + ["running"] * 20 + ["failed"] * 15 + ["cancelled"] * 5
    random.shuffle(PJ_STATUSES)
    filenames = [f"{m[0].lower().replace(' ', '_').replace('(', '').replace(')', '')}.3mf" for m in MODELS]

    for i in range(200):
        pj_status = PJ_STATUSES[i]
        pid = random.choice(bambu_pids)
        fname = random.choice(filenames)
        total_layers = random.randint(50, 300)
        started = rand_dt(30, 0)

        if pj_status == "completed":
            duration_min = random.uniform(20, 360)
            ended = started + timedelta(minutes=duration_min)
            progress = 100.0
            cur_layer = total_layers
        elif pj_status == "running":
            progress = round(random.uniform(5, 90), 1)
            cur_layer = int(total_layers * progress / 100)
            ended = None
            duration_min = None
        elif pj_status == "failed":
            progress = round(random.uniform(5, 80), 1)
            cur_layer = int(total_layers * progress / 100)
            duration_min = random.uniform(10, 200)
            ended = started + timedelta(minutes=duration_min)
        else:  # cancelled
            progress = round(random.uniform(0, 30), 1)
            cur_layer = int(total_layers * progress / 100)
            duration_min = random.uniform(5, 60)
            ended = started + timedelta(minutes=duration_min)

        conn.execute("""
            INSERT INTO print_jobs (
                printer_id, job_id, filename, job_name, started_at, ended_at,
                status, progress_percent, remaining_minutes,
                total_layers, current_layer,
                bed_temp_target, nozzle_temp_target, error_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            pid, f"bambu_{random.randint(10000,99999)}", fname,
            fname.replace(".3mf", "").replace("_", " ").title(),
            iso(started), iso(ended) if ended else None,
            pj_status, progress,
            random.uniform(10, 180) if pj_status == "running" else 0,
            total_layers, cur_layer,
            random.choice([55.0, 60.0, 65.0]),
            random.choice([200.0, 215.0, 220.0, 250.0]),
            "0500_0100_0001_0001" if pj_status == "failed" else None,
        ))
    conn.commit()
    print(f"  ✓ 200 print_jobs")

    # ── Printer Telemetry (~5000 rows, 48h for active printers) ──
    active_pids = [printer_ids[i] for i, p in enumerate(PRINTERS) if STATES_POOL[i] != "OFFLINE"]
    telemetry_count = 0
    # 48 hours, every 5 min = 576 points per printer. ~19 active printers = ~10,900. Let's do every 15min = ~3600.
    interval_min = 15
    points_per_printer = int(48 * 60 / interval_min)

    for pid_idx, pid in enumerate(active_pids):
        p_state = STATES_POOL[printer_ids.index(pid)]
        # Create print windows: printing for some hours, then idle
        is_printing_now = p_state == "RUNNING"
        batch = []
        for pt in range(points_per_printer):
            t = NOW - timedelta(minutes=pt * interval_min)
            # Simulate: printing during working hours (8am-10pm), idle otherwise
            hour = (t.hour + random.uniform(-0.5, 0.5)) % 24
            is_printing = (8 <= hour <= 22) and (random.random() > 0.2)
            if pt < 4:  # last hour matches actual state
                is_printing = is_printing_now

            if is_printing:
                bed = round(random.uniform(55, 65), 1)
                nozzle = round(random.uniform(200, 260), 1)
                fan = random.randint(40, 100)
            else:
                bed = round(random.uniform(22, 28), 1)
                nozzle = round(random.uniform(22, 28), 1)
                fan = 0

            batch.append((pid, bed, nozzle,
                          60.0 if is_printing else 0,
                          215.0 if is_printing else 0,
                          fan, iso(t)))

        conn.executemany(
            "INSERT INTO printer_telemetry (printer_id, bed_temp, nozzle_temp, bed_target, nozzle_target, fan_speed, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", batch
        )
        telemetry_count += len(batch)

    conn.commit()
    print(f"  ✓ {telemetry_count} telemetry rows")

    # ── HMS Error History ──
    print("\n[10/14] Seeding HMS errors, nozzle lifecycle, alerts...")
    for _ in range(25):
        pid = random.choice(bambu_pids)
        code, msg, severity = random.choice(HMS_ERRORS)
        conn.execute(
            "INSERT INTO hms_error_history (printer_id, code, message, severity, source, occurred_at) "
            "VALUES (?, ?, ?, ?, 'bambu_hms', ?)",
            (pid, code, msg, severity, iso(rand_dt(60, 0)))
        )
    conn.commit()
    print(f"  ✓ 25 HMS errors")

    # ── Nozzle Lifecycle ──
    nozzle_types = ["hardened_steel", "stainless_steel", "brass", "hardened_steel"]
    nozzle_diameters = [0.4, 0.4, 0.4, 0.6]
    nozzle_count = 0
    for pid in printer_ids:
        # 1 retired nozzle
        nt_idx = random.randint(0, 3)
        installed = rand_dt(365, 120)
        removed = installed + timedelta(days=random.randint(30, 200))
        conn.execute("""
            INSERT INTO nozzle_lifecycle (
                printer_id, nozzle_type, nozzle_diameter,
                installed_at, removed_at, print_hours_accumulated, print_count, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            pid, nozzle_types[nt_idx], nozzle_diameters[nt_idx],
            iso(installed), iso(removed),
            round(random.uniform(200, 800), 1), random.randint(50, 300),
            "Replaced due to wear"
        ))
        nozzle_count += 1
        # 1 current nozzle
        nt_idx2 = random.randint(0, 3)
        conn.execute("""
            INSERT INTO nozzle_lifecycle (
                printer_id, nozzle_type, nozzle_diameter,
                installed_at, removed_at, print_hours_accumulated, print_count
            ) VALUES (?, ?, ?, ?, NULL, ?, ?)
        """, (
            pid, nozzle_types[nt_idx2], nozzle_diameters[nt_idx2],
            iso(rand_dt(60, 1)),
            round(random.uniform(10, 150), 1), random.randint(5, 60),
        ))
        nozzle_count += 1
    conn.commit()
    print(f"  ✓ {nozzle_count} nozzle lifecycle entries")

    # ── Alerts ──
    printer_names = [p[0] for p in PRINTERS]
    for _ in range(30):
        tmpl = random.choice(ALERT_TEMPLATES)
        atype, severity, title, msg_tmpl = tmpl
        pid = random.choice(printer_ids)
        pname = printer_names[printer_ids.index(pid)]
        if "Completed" in title or "completed" in title:
            msg = f"Print job '{random.choice([m[0] for m in MODELS])}' completed on {pname}"
        elif "Failed" in title:
            msg = f"Print job '{random.choice([m[0] for m in MODELS])}' failed on {pname}"
        elif "Spool" in title or "Filament" in title:
            msg = f"Filament spool running low ({random.randint(10, 80)}g remaining)"
        else:
            msg = f"{pname} is overdue for maintenance"

        conn.execute("""
            INSERT INTO alerts (
                user_id, alert_type, severity, title, message,
                is_read, is_dismissed, printer_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
        """, (
            random.randint(1, len(USERS)),
            atype, severity, title, msg,
            random.choice([0, 0, 1]), pid, iso(rand_dt(14, 0)),
        ))
    conn.commit()
    print(f"  ✓ 30 alerts")

    # ── Audit Logs ──
    print("\n[11/14] Seeding audit logs...")
    usernames = [u[0] for u in USERS]
    for _ in range(100):
        action = random.choice(AUDIT_ACTIONS)
        entity = random.choice(AUDIT_ENTITIES)
        user = random.choice(usernames)
        if action == "login":
            details = json.dumps({"username": user, "method": "password"})
        elif action == "create":
            details = json.dumps({"name": f"New {entity}", "created_by": user})
        elif action == "update":
            details = json.dumps({"field": "status", "old": "pending", "new": "completed", "by": user})
        elif action == "delete":
            details = json.dumps({"name": f"Deleted {entity}", "by": user})
        else:
            details = json.dumps({"format": "csv", "rows": random.randint(10, 500), "by": user})

        conn.execute("""
            INSERT INTO audit_logs (timestamp, action, entity_type, entity_id, details, ip_address)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            iso(rand_dt(30, 0)), action, entity,
            str(random.randint(1, 50)), details,
            f"192.168.1.{random.randint(2, 254)}",
        ))
    conn.commit()
    print(f"  ✓ 100 audit logs")

    # ── Maintenance Tasks & Logs ──
    print("\n[12/14] Seeding maintenance tasks & logs...")
    mt_ids = []
    for name, desc, printer_filter, interval_h, interval_d, cost, downtime in MAINTENANCE_TASKS:
        conn.execute("""
            INSERT INTO maintenance_tasks (
                name, description, printer_model_filter,
                interval_print_hours, interval_days,
                estimated_cost, estimated_downtime_min, is_active, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
        """, (name, desc, printer_filter, interval_h, interval_d, cost, downtime,
              iso(rand_dt(180, 90))))
        mt_ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()

    # 40 maintenance logs
    performers = ["sarah", "mike", "demo"]
    for _ in range(40):
        pid = random.choice(printer_ids)
        task = random.choice(mt_ids)
        task_idx = mt_ids.index(task)
        task_name = MAINTENANCE_TASKS[task_idx][0]
        conn.execute("""
            INSERT INTO maintenance_logs (
                printer_id, task_id, task_name, performed_at, performed_by,
                notes, cost, downtime_minutes, print_hours_at_service
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            pid, task, task_name, iso(rand_dt(90, 1)),
            random.choice(performers),
            f"Routine {task_name.lower()} completed",
            round(random.uniform(0, 15), 2),
            random.randint(5, 60),
            round(random.uniform(50, 500), 1),
        ))
    conn.commit()
    print(f"  ✓ {len(MAINTENANCE_TASKS)} tasks, 40 logs")

    # ── Consumables ──
    print("\n[13/14] Seeding consumables...")
    con_ids = []
    for name, sku, unit, cost, stock, min_stock, vendor in CONSUMABLES:
        conn.execute("""
            INSERT INTO consumables (
                name, sku, unit, cost_per_unit, current_stock, min_stock,
                vendor, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
        """, (name, sku, unit, cost, stock, min_stock, vendor,
              iso(rand_dt(120, 30)), iso(NOW)))
        con_ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    # Product-consumable links (~20)
    pc_count = 0
    for prod_id in product_ids:
        n_cons = random.randint(1, 3)
        chosen = random.sample(con_ids, min(n_cons, len(con_ids)))
        for cid in chosen:
            conn.execute(
                "INSERT INTO product_consumables (product_id, consumable_id, quantity_per_product) VALUES (?, ?, ?)",
                (prod_id, cid, round(random.uniform(0.1, 2.0), 2))
            )
            pc_count += 1
    conn.commit()
    print(f"  ✓ {len(CONSUMABLES)} consumables, {pc_count} product links")

    # ── License ──
    print("\n[14/14] Generating enterprise license...")
    generate_license()

    conn.execute("PRAGMA foreign_keys=ON")
    conn.close()

    print("\n" + "=" * 60)
    print("Done! Dataset summary:")
    print(f"  Users:        {len(USERS)}")
    print(f"  Printers:     {len(PRINTERS)}")
    print(f"  Filament Lib: {len(FILAMENT_LIBRARY)}")
    print(f"  Spools:       25")
    print(f"  Fil. Slots:   {slot_count}")
    print(f"  Models:       {len(MODELS)}")
    print(f"  Products:     {len(PRODUCTS)}")
    print(f"  Orders:       50 ({total_items} items)")
    print(f"  Jobs:         500")
    print(f"  Print Jobs:   200")
    print(f"  Telemetry:    {telemetry_count}")
    print(f"  HMS Errors:   25")
    print(f"  Nozzles:      {nozzle_count}")
    print(f"  Alerts:       30")
    print(f"  Audit Logs:   100")
    print(f"  Maint Tasks:  {len(MAINTENANCE_TASKS)} + 40 logs")
    print(f"  Consumables:  {len(CONSUMABLES)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
