"""Owned application database setup for actual dispatch boundary tests."""
import hashlib,secrets,zipfile
from sqlalchemy import text


BACKGROUND_THREADS=[]


def join_background_threads(timeout=10.0):
    while BACKGROUND_THREADS:
        BACKGROUND_THREADS.pop().join(timeout)


def configure_dispatch(db,tmp_path,monkeypatch,protocol='moonraker'):
    from core import auth,crypto,db as core_db
    from core.registry import registry
    from cryptography.fernet import Fernet
    from modules.organizations.services import EducationPolicyService
    from modules.printers import dispatch,smart_plug
    from modules.printers import dispatch_acquisition
    monkeypatch.setattr(auth,'SECRET_KEY',secrets.token_hex(32))
    monkeypatch.setenv('ENCRYPTION_KEY',Fernet.generate_key().decode())
    engine=db.get_bind()
    monkeypatch.setattr(core_db,'engine',engine)
    monkeypatch.setattr(smart_plug,'engine',engine)
    # No smart-plug energy thread may outlive this test (it would read the next
    # test's monkeypatched engine): start immediately and join at teardown.
    monkeypatch.setattr(smart_plug,'_ENERGY_START_DELAY',0)
    original_background=smart_plug._in_background
    def tracked(function,*args):
        worker=original_background(function,*args)
        BACKGROUND_THREADS.append(worker)
        return worker
    monkeypatch.setattr(smart_plug,'_in_background',tracked)
    monkeypatch.setitem(registry._providers,'EducationPolicyProvider',EducationPolicyService())
    monkeypatch.setattr(dispatch,'_ws',lambda *args:None)
    monkeypatch.setattr(dispatch,'_notify_start_outcome',lambda *args:None)
    monkeypatch.setattr(dispatch_acquisition,'bounded_status_is_idle',lambda *args:True)
    path=tmp_path/('synthetic.3mf' if protocol=='bambu' else 'synthetic.gcode')
    if protocol=='bambu':
        with zipfile.ZipFile(path,'w') as package:package.writestr('Metadata/plate_1.gcode','G90\n')
    else:path.write_bytes(b'G90\n')
    credential=crypto.encrypt('SYNTHETICPRINTER|'+secrets.token_hex(16)) if protocol=='bambu' else ''
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO printers(id,name,is_active,api_type,api_host,api_key,org_id,bed_x_mm,bed_y_mm,nozzle_diameter,gcode_state) VALUES(59,'Owned dispatch test',TRUE,:protocol,'127.0.0.1',:key,1,256,256,.4,'IDLE')"),{'protocol':protocol,'key':credential})
        conn.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(59,1,'PLA')"))
        conn.execute(text("INSERT INTO models(id,name,org_id) VALUES(51,'Owned dispatch test',1)"))
        conn.execute(text("INSERT INTO print_files(id,model_id,filename,original_filename,stored_path,org_id) VALUES(52,51,:filename,:filename,:path,1)"),{'filename':path.name,'path':str(path)})
        for identifier in (53,54):
            conn.execute(text("INSERT INTO jobs(id,model_id,item_name,status,printer_id,charged_to_org_id,hold) VALUES(:id,51,'Owned dispatch test','scheduled',59,1,FALSE)"),{'id':identifier})
        if protocol=='bambu':
            conn.execute(text('UPDATE jobs SET print_file_id=52,payload_sha256=:hash,plate_index=0 WHERE id IN (53,54)'),{'hash':hashlib.sha256(path.read_bytes()).hexdigest()})
    return engine,path
