import importlib
from sqlalchemy import create_engine,text
from core.schema import bootstrap_database
M=importlib.import_module("core.schema.migrations.025_spool_purchase_evidence")

def test_legacy024_upgrade_preserves_raw_prices_without_purchase_adoption(tmp_path,monkeypatch):
    import core.schema.bootstrap as bootstrap
    from core.economic_source_creation import _create_spool
    from sqlalchemy.orm import Session
    engine=create_engine('sqlite:///'+str(tmp_path/'legacy24.db'))
    try:
        # Construct exact prior registered024 fixture without altering any frozen
        # migration/ORM source or dropping installed025 guards/columns.
        with monkeypatch.context() as patch:
            real=bootstrap.apply_python_migration
            patch.setattr(bootstrap,'apply_python_migration',lambda c,name:False if name.endswith('.025_spool_purchase_evidence') else real(c,name))
            patch.setattr(bootstrap,'RAW_REQUIRED_TABLES',bootstrap.RAW_REQUIRED_TABLES-{'spool_purchase_evidence'})
            maps=dict(bootstrap.RAW_REQUIRED_COLUMNS);maps.pop('spool_purchase_evidence');maps['spools']=maps['spools']-{'purchase_revision','purchase_evidence_nonce'}
            patch.setattr(bootstrap,'RAW_REQUIRED_COLUMNS',maps)
            bootstrap.bootstrap_database(engine)
        with Session(engine) as db:
            db.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(80,'Owned','Fixture','PLA')"))
            known=_create_spool(db,{'filament_id':80,'org_id':1,'price':0,'initial_weight_g':1000,'status':'active'},owner_org_id=1)
            unknown=_create_spool(db,{'filament_id':80,'price':20,'initial_weight_g':1000,'status':'active'})
            db.commit()
        result=bootstrap_database(engine);assert result['applied']==[M.MIGRATION_ID]
        with engine.connect() as c:
            assert c.execute(text('SELECT purchase_evidence_nonce,purchase_revision FROM spools ORDER BY id')).all()==[(None,None),(None,None)]
            assert c.execute(text('SELECT price,initial_weight_g FROM spools ORDER BY id')).all()==[(0,1000),(20,1000)]
            assert c.execute(text('SELECT count(*) FROM spool_purchase_evidence')).scalar_one()==0
            assert c.execute(text('SELECT economic_uid FROM spools WHERE id=:id'),{'id':unknown.id}).scalar_one() is None
    finally:engine.dispose()
