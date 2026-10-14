"""Fresh actual application bootstrap in an owned PostgreSQL database."""
import json
import sys
from sqlalchemy import create_engine
sys.path.insert(0, '/app/tests/test_contracts')
from test_timelapse_registration_acceptance import verify_registration
engine = create_engine('postgresql+psycopg://postgres@'+sys.argv[1]+'/postgres')
try:
    print(json.dumps(verify_registration(engine)))
finally:
    engine.dispose()
