"""Synthetic review data, confined to a disposable database. No board sample values.

Feeding and do-not-feed are NOT seeded: neither state exists in the HTTP slot
schema. Their presentation is covered by the six-state component test. Camera
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
    # The seed explicitly records its unsupported surfaces for screenshot reviewers.
    return {'printers': len(states), 'supported_slot_states': ['loaded', 'empty', 'changed', 'support'],
            'unsupported_slot_states': ['feeding', 'blocked'], 'camera_transport': 'unavailable',
            'now': now.isoformat()}
