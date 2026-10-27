"""Purpose-separated dispatch evidence; never stores raw credentials or names."""
import hashlib
import hmac
import json
import re
from dataclasses import asdict
from sqlalchemy import insert,select
from core.dispatch_claim_schema import PrinterDispatchClaim
from core.recipe_configuration_schema import DispatchAttemptReceipt


def _fingerprint(key,purpose,values):
    if type(key) is not bytes or len(key)<32:
        raise ValueError('Dispatch receipt key unavailable')
    encoded=json.dumps(values,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    return hmac.new(key,purpose.encode()+b'\x00'+encoded,hashlib.sha256).hexdigest()


def generic_filename_token(filename):
    if type(filename) is not str:
        return None
    matched=re.fullmatch(r'odin-dispatch-([0-9a-f]{32})\.(?:gcode|bgcode|3mf)',filename)
    return matched.group(1) if matched else None


def store_admission_receipt(conn,*,token,job,creds,key,configuration_key,filename,broker_connection,power_accepted):
    """Insert only for this transaction's exact adapter-entered claim."""
    if power_accepted is not True:
        raise ValueError('Dispatch power preparation unconfirmed')
    from core.interfaces.education_policy import parse_education_token
    if job.get('education_submission_id') is None:
        if generic_filename_token(filename)!=token:
            raise ValueError('Dispatch filename identity differs')
    elif parse_education_token(filename) is None:
        raise ValueError('Education filename identity unavailable')
    claim=PrinterDispatchClaim.__table__
    owned=conn.execute(select(claim.c.token).where(
        claim.c.token==token,claim.c.printer_id==job['printer_id'],claim.c.job_id==job['id'],
        claim.c.configuration_key==configuration_key,claim.c.state=='adapter_entered')).first()
    if owned is None:
        raise ValueError('Dispatch receipt claim identity differs')
    from core.dispatch_identity import stable_configuration_key
    stable = stable_configuration_key(creds, key)
    if stable is None:
        raise ValueError("Recipe configuration identity unavailable")
    conn.execute(insert(DispatchAttemptReceipt.__table__).values(
        claim_token=token,printer_id=job['printer_id'],job_id=job['id'],configuration_key=configuration_key,
        stable_configuration_key=stable,stable_key_source='dispatch',
        prepared_power_key=power_fingerprint(key,token,creds['plug_configuration'],broker_connection),
        observed_filename_key=filename_fingerprint(key,token,filename)))


def power_fingerprint(key,token,plug,broker_connection):
    # All inputs remain ephemeral, including raw stored plug credentials.
    broker=asdict(broker_connection) if broker_connection is not None else None
    return _fingerprint(key,'odin-dispatch-power-v1',{'token':token,'plug':plug,'broker':broker})


def filename_fingerprint(key,token,filename):
    return _fingerprint(key,'odin-dispatch-filename-v1',{'token':token,'filename':filename})


def _observed_names(protocol,name):
    if type(name) is not str:return ()
    names=(name,)
    # Bambu's subtask_name omits the upload extension. Reconstruct only our
    # complete nonce namespace, never user names, paths or explicit suffixes.
    if protocol=='bambu' and re.fullmatch(r'odin-(?:dispatch-)?[0-9a-f]{32}',name):
        names+=tuple(name+suffix for suffix in ('.3mf','.gcode','.bgcode'))
    return names


def generic_observed_token(protocol,name):
    for candidate in _observed_names(protocol,name):
        token=generic_filename_token(candidate)
        if token is not None:return token
    return None


def observed_filename_matches(key,token,protocol,name,expected):
    """Match actual wire name to immutable admission; never guess past HMAC."""
    if type(expected) is not str or re.fullmatch('[0-9a-f]{64}',expected) is None:return False
    return any(hmac.compare_digest(expected,filename_fingerprint(key,token,candidate))
               for candidate in _observed_names(protocol,name))
