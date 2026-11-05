"""Synthetic review data, confined to a disposable database. No board sample values.

Feeding, chamber, next-nozzle binding and plate confirmation are seeded through
the real display cache/timeline. Do-not-feed remains component-test-only. Camera
availability is seeded; the fixture deliberately has no physical camera transport.
"""
import json
from datetime import datetime, timedelta, timezone
from sqlalchemy import text


def seed(connection, password_hash, now=None):
    now = now or datetime.now(timezone.utc)
    stamp = lambda minutes=0: (now + timedelta(minutes=minutes)).replace(tzinfo=None).isoformat()
    connection.execute(text("INSERT INTO users (id,username,email,password_hash,role,is_active,group_id) VALUES (1,'reviewer','reviewer@example.test',:password,'admin',1,NULL)"), {'password': password_hash})
    connection.execute(text("INSERT INTO system_config (key,value) VALUES ('setup_complete','true')"))
    from core.crypto import encrypt
    states = [
        (101, 'Review printer · printing', 'X1 Carbon', 'X1C', 'bambu', 'RUNNING', 221, 62),
        (102, 'Review printer · paused', 'A1', 'A1', 'bambu', 'PAUSE', 183, 59),
        (103, 'Review printer · ready', 'H2D', 'H2D', 'bambu', 'IDLE', 29, 26),
        (104, 'Review printer · error', 'Kobra S1', 'Kobra S1', 'anycubic_lan', 'ERROR', 32, 27),
        (105, 'Review printer · offline', 'MK4', 'MK4', 'prusalink', 'OFFLINE', None, None),
        (106, 'Review printer · unknown', None, None, None, None, None, None),
    ]
    for pid, name, model, machine, protocol, state, nozzle, bed in states:
        connection.execute(text("""INSERT INTO printers
          (id,name,model,machine_type,api_type,api_host,is_active,slot_count,gcode_state,last_seen,
           nozzle_temp,bed_temp,nozzle_1_temp,camera_url,camera_enabled,tags,shared,timelapse_enabled)
          VALUES (:id,:name,:model,:machine,:protocol,'127.0.0.1:1',1,4,:state,:seen,:nozzle,:bed,:left,
                  :camera,:enabled,'[]',0,0)"""),
          dict(id=pid, name=name, model=model, machine=machine, protocol=protocol, state=state,
               seen=stamp(-60 if pid == 105 else 0) if pid != 106 else None,
               nozzle=nozzle, bed=bed, left=31 if pid == 103 else None,
               camera='http://127.0.0.1:1/fixture-camera-unavailable' if pid in (101, 102, 103) else None,
               enabled=pid in (101, 102, 103)))
        for slot, material, color, name in [(1, 'PLA', '508BCA', 'Review blue PLA with an intentionally long inventory label'), (2, 'empty', None, None), (3, 'PETG', 'A54D35', 'Review changed spool'), (4, 'PLA_SUPPORT', 'DADADA', 'Review support material')]:
            observation = dict(id=f'fixture-{pid}-{slot}', pending=slot == 3,
                               observed=dict(type=material, sub_brand='Review preset', exists=slot != 2),
                               remaining=67 if slot == 1 else None)
            connection.execute(text("""INSERT INTO filament_slots
              (printer_id,slot_number,filament_type,color,color_hex,spool_confirmed,tray_observation)
              VALUES (:pid,:slot,:material,:name,:color,0,:observation)"""),
              dict(pid=pid, slot=slot, material=material, name=name, color=color,
                   observation=json.dumps(observation) if protocol == 'bambu' else None))
    connection.execute(text("INSERT INTO filament_slots (printer_id,slot_number,filament_type,color,color_hex) VALUES (103,17,'SUPPORT','Review HT support','BFBFBF')"))
    connection.execute(text("UPDATE printers SET api_key=:key WHERE api_type='bambu'"), {'key': encrypt('fixture-serial|fixture-access-code')})
    connection.execute(text("INSERT INTO filament_library (id,brand,name,material,color_hex,is_custom) VALUES (301,'Review','Blue PLA with a deliberately long inventory label','PLA','508BCA',1)"))
    for sid, pid in [(401, 101), (402, 102), (403, 103)]:
        connection.execute(text("INSERT INTO spools (id,filament_id,qr_code,initial_weight_g,remaining_weight_g,status,location_printer_id,location_slot) VALUES (:id,301,:qr,1000,670,'active',:pid,1)"), dict(id=sid, pid=pid, qr=f'fixture-spool-{sid}'))
        connection.execute(text("UPDATE filament_slots SET assigned_spool_id=:sid,spool_confirmed=1 WHERE printer_id=:pid AND slot_number=1"), dict(sid=sid, pid=pid))
    for pid, pct, layer in [(101, 37, 74), (102, 58, 116)]:
        connection.execute(text("""INSERT INTO print_jobs (printer_id,job_name,status,started_at,progress_percent,current_layer,total_layers,remaining_minutes)
          VALUES (:pid,:name,'running',:started,:pct,:layer,200,83)"""),
          dict(pid=pid, name=f'Review bracket {pid}.3mf', started=stamp(-45), pct=pct, layer=layer))
    for jid, pid, name, status, hold, start, end in [
        (201, 101, 'Review queued enclosure', 'scheduled', False, 90, 210),
        (202, 103, 'Review held calibration', 'pending', True, None, None),
        (203, 103, 'Review next fixture', 'scheduled', False, 15, 85),
    ]:
        connection.execute(text("""INSERT INTO jobs (id,printer_id,item_name,status,hold,scheduled_start,scheduled_end,duration_hours,priority,quantity,ams_mapping)
          VALUES (:id,:pid,:name,:status,:hold,:start,:end,1.2,3,1,:mapping)"""),
          dict(id=jid, pid=pid, name=name, status=status, hold=hold, start=stamp(start) if start is not None else None,
               end=stamp(end) if end is not None else None, mapping='[0]' if pid == 103 else None))
    for pid, kind, reason in [(102, 'print_paused', 'Filament sensor needs attention.'), (104, 'printer_error', 'The printer reported a motion error.')]:
        connection.execute(text("""INSERT INTO alerts (user_id,alert_type,severity,title,message,is_read,is_dismissed,printer_id,created_at,metadata_json)
          VALUES (1,:kind,'warning',:title,:message,0,0,:pid,:created,:metadata)"""),
          dict(kind=kind, pid=pid, title=f'Review printer {pid} needs you', message=reason,
               created=stamp(-7), metadata=json.dumps({'pause_reason': reason})))
    # Synthetic monitor observations use the same parser/cache shape as production.
    from types import SimpleNamespace
    from core.printer_display_facts import observe, cache_key
    for pid, packet in [(101, {'ams': {'tray_now': '0'}, 'chamber_temper': 36}),
                        (103, {'device': {'ctc': {'info': {'temp': 30}}}, 'ams': {'ams': [
                            {'id': '0', 'info': '000', 'tray': [{'id': '0', 'tray_type': 'PLA'}]}]}})]:
        monitor = SimpleNamespace()
        observe(monitor, packet)
        for fact in monitor._display_facts.values():
            fact['at'] = now.timestamp()
        created = connection.execute(text('SELECT created_at FROM printers WHERE id=:id'), {'id': pid}).scalar_one()
        connection.execute(text('INSERT INTO system_config(key,value) VALUES(:key,:value)'),
            {'key': cache_key(pid), 'value': json.dumps({'printer_created_at': str(created), 'facts': monitor._display_facts})})
    connection.execute(text("INSERT INTO job_timeline_events(id,job_id,printer_id,kind,detail,occurred_at) VALUES('foundry-clear',203,103,'bed_clear_confirmed',:detail,:at)"),
        {'detail': json.dumps({'attestation': 'owner_in_app'}), 'at': stamp(-9)})
    page_records = seed_pages(connection, stamp)
    # The seed explicitly records its unsupported surfaces for screenshot reviewers.
    return {'page_records': page_records, 'printers': len(states), 'supported_slot_states': ['loaded', 'feeding', 'empty', 'changed', 'support'],
            'unsupported_slot_states': ['blocked'], 'camera_transport': 'unavailable',
            'now': now.isoformat()}


def seed_pages(connection, stamp):
    """Persisted review records for Part B. No media files or printable payloads
    are fabricated: failed historical recordings and archives have no media path.
    Empty upload/submission states remain honest until a real file is supplied.
    """
    statements = [
        ("models", """INSERT INTO models (id,name,build_time_hours,default_filament_type,color_requirements,category,notes,units_per_bed,quantity_per_bed,is_favorite,created_at)
           VALUES (501,'Review enclosure',1.5,'PLA','{}','Review parts','Synthetic screenshot fixture',1,1,0,:at)"""),
        ("products", """INSERT INTO products(id,name,sku,price,description,created_at)
           VALUES (601,'Review enclosure kit','REVIEW-601',18,'Synthetic screenshot fixture',:at)"""),
        ("product_components", """INSERT INTO product_components(product_id,model_id,quantity_needed) VALUES (601,501,2)"""),
        ("orders", """INSERT INTO orders(id,order_number,platform,customer_name,status,revenue,platform_fees,payment_fees,shipping_charged,shipping_cost,labor_minutes,order_date,created_at)
           VALUES (701,'REVIEW-701','direct','Review customer','pending',36,0,0,0,0,0,:at,:at)"""),
        ("order_items", """INSERT INTO order_items(order_id,product_id,quantity,unit_price,fulfilled_quantity) VALUES (701,601,2,18,0)"""),
        ("consumables", """INSERT INTO consumables(id,name,sku,unit,cost_per_unit,current_stock,min_stock,status,created_at)
           VALUES (801,'Review M3 fasteners','REVIEW-801','piece',0.08,24,30,'active',:at)"""),
        ("projects", """INSERT INTO projects(id,created_by,name,description,color,status,expected_parts,created_at,updated_at)
           VALUES (901,1,'Review workshop fixtures','Synthetic screenshot fixture','#508BCA','active',4,:at,:at)"""),
        ("print_jobs", """INSERT INTO print_jobs(id,printer_id,job_name,status,started_at,ended_at,progress_percent,current_layer,total_layers)
           VALUES (1001,101,'Review completed bracket.3mf','completed',:start,:at,100,200,200)"""),
        ("print_archives", """INSERT INTO print_archives(id,print_job_id,printer_id,user_id,print_name,status,started_at,completed_at,actual_duration_seconds,filament_used_grams,cost_estimate,notes,created_at,project_id,tags)
           VALUES (1101,1001,101,1,'Review completed bracket','completed',:start,:at,5400,32,1.2,'No archived file in this fixture',:at,901,'review')"""),
        ("timelapses", """INSERT INTO timelapses(id,printer_id,print_job_id,filename,frame_count,status,created_at)
           VALUES (1201,101,1001,'review-unavailable.mp4',0,'failed',:at)"""),
        ("printer_profiles", """INSERT INTO printer_profiles(id,created_by,printer_id,name,description,slicer,category,raw_content,is_shared,is_default,created_at,updated_at)
           VALUES (1301,1,101,'Review PLA process','Synthetic screenshot fixture','orca','process','{"layer_height": "0.2"}',1,0,:at,:at)"""),
        ("vision_detections", """INSERT INTO vision_detections(id,printer_id,print_job_id,detection_type,confidence,status,created_at)
           VALUES (1401,101,1001,'spaghetti',0.87,'pending',:at)"""),
        ("maintenance_tasks", """INSERT INTO maintenance_tasks(id,name,description,interval_days,estimated_cost,estimated_downtime_min,is_active,created_at)
           VALUES (1501,'Review belt inspection','Synthetic screenshot fixture',30,0,15,1,:at)"""),
        ("maintenance_logs", """INSERT INTO maintenance_logs(printer_id,task_id,task_name,performed_at,performed_by,notes,cost,downtime_minutes,print_hours_at_service)
           VALUES (101,1501,'Review belt inspection',:at,'reviewer','Synthetic screenshot fixture',0,15,10)"""),
        ("groups", """INSERT INTO groups(id,name,description,owner_id,is_org,settings_json,created_at,updated_at)
           VALUES (1601,'Review education workshop','Synthetic screenshot fixture',1,1,'{}',:at,:at)"""),
        ("education_cost_centers", """INSERT INTO education_cost_centers(id,org_id,name_key,code_key,display_name,code,description,created_by,created_at,updated_at)
           VALUES (1701,1601,'review workshop','review','Review workshop','REVIEW','No submissions or fabricated files',1,:at,:at)"""),
    ]
    for table, statement in statements:
        connection.execute(text(statement), {'at': stamp(-120), 'start': stamp(-210)})
    return [table for table, _ in statements]
