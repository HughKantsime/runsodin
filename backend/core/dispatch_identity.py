"""Configured-target identity and keyed configuration equality, without I/O.

Origins and claimed Bambu serials do not establish physical hardware identity.
DNS aliases and alternate origins remain distinct configured targets.
"""
from dataclasses import dataclass
import hashlib
import hmac
import ipaddress
import json
import math
import re


@dataclass(frozen=True)
class DispatchIdentity:
    target_key: str
    configuration_key: str


def _host(value):
    if not isinstance(value,str) or not value or value!=value.strip() or len(value)>253 or any(char.isspace() or ord(char)<32 for char in value):
        raise ValueError('Invalid host')
    if '%' in value:
        raise ValueError('Scoped address is not supported')
    bracketed=value.startswith('[') or value.endswith(']')
    if bracketed:
        if not (value.startswith('[') and value.endswith(']')):
            raise ValueError('Invalid address brackets')
        address=ipaddress.ip_address(value[1:-1])
        if address.version!=6:
            raise ValueError('Brackets require IPv6')
        return address.compressed.lower()
    try:
        return ipaddress.ip_address(value).compressed.lower()
    except ValueError:
        normalized=(value[:-1] if value.endswith('.') else value).lower()
        if not normalized or re.fullmatch(r'[0-9.]+',normalized) or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',label) for label in normalized.split('.')):
            raise ValueError('Invalid hostname')
        return normalized


def ownership_refusal(job,creds):
    """The charged job tenant, not an absent principal, controls private inputs."""
    if not isinstance(job,dict) or not isinstance(creds,dict):return 'Dispatch ownership could not be verified'
    tenant=job.get('charged_to_org_id')
    owners=(tenant,job.get('model_org_id'),job.get('print_file_org_id'),creds.get('org_id'))
    if any(value is not None and (type(value) is not int or value<=0) for value in owners):
        return 'Dispatch ownership could not be verified'
    if any(owner is not None and owner!=tenant for owner in owners[1:3]):
        return 'Private payload resources require an explicit matching job tenant'
    if owners[3] is not None and owners[3]!=tenant and creds.get('shared') is not True:
        return 'Private printer requires an explicit matching job tenant or sharing'
    return None


def build_identity(creds,key,*,_recipe=False):
    """Return equality keys only; malformed inputs refuse without echoing them."""
    try:
        if not isinstance(creds,dict) or not isinstance(key,bytes) or len(key)<32:
            return None
        protocol=creds.get('api_type')
        if protocol not in ('bambu','moonraker','prusalink'):
            return None
        host=_host(creds.get('ip'))
        port=creds.get('port',80)
        if isinstance(port,str) and re.fullmatch(r'[0-9]{1,5}',port):port=int(port)
        if type(port) is not int or not 1<=port<=65535:
            return None
        scheme=creds.get('scheme','http')
        if scheme not in ('http','https'):
            return None
        config={'api_type':protocol,'host':host,'port':port,'scheme':scheme}
        if protocol=='bambu':
            serial=creds.get('serial')
            if not isinstance(serial,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',serial):
                return None
            config['serial']=serial
            target=['bambu-serial',serial.upper()]
        else:target=['http-origin',scheme,host,port]
        for name in ('access_code','api_key','username','password'):
            value=creds.get(name,'maker' if name=='username' and protocol=='prusalink' else '')
            if not isinstance(value,str):return None
            config[name]=value
        owner=creds.get('org_id')
        if owner is not None and (type(owner) is not int or owner<=0):return None
        active=creds.get('is_active')
        if type(active) is not bool:return None
        shared=creds.get('shared',False)
        if type(shared) is not bool:return None
        config.update(org_id=owner,is_active=active,shared=shared)
        for name in ('bed_x_mm','bed_y_mm','nozzle_diameter'):
            value=creds.get(name)
            if value is not None:
                if isinstance(value,bool) or not isinstance(value,(int,float)):return None
                value=float(value)
                if not math.isfinite(value) or value<0:return None
            config[name]=value
        for name in ('machine_type','model'):
            value=creds.get(name)
            if value is not None and not isinstance(value,str):return None
            config[name]=value
        materials=creds.get('active_materials',[])
        if not isinstance(materials,list) or len(materials)>128 or any(not isinstance(value,str) or len(value)>128 for value in materials):return None
        config['active_materials']=sorted(set(materials))
        slots=creds.get('filament_slots',[])
        if not isinstance(slots,list) or len(slots)>256:return None
        if any(not isinstance(slot,(list,tuple)) or len(slot)!=2 or type(slot[0]) is not int or not 1<=slot[0]<=256 or not isinstance(slot[1],str) or len(slot[1])>128 for slot in slots):return None
        if len({slot[0] for slot in slots})!=len(slots):return None
        config['filament_slots']=sorted([list(slot) for slot in slots],key=lambda slot:slot[0])
        plug=creds.get('plug_configuration')
        if plug is not None:
            if not isinstance(plug,dict):return None
            normalized_plug={}
            for name in ('plug_type','plug_host','plug_entity_id','plug_auth_token'):
                value=plug.get(name)
                if value is not None and not isinstance(value,str):return None
                normalized_plug[name]=value
            auto_on=plug.get('plug_auto_on')
            if auto_on is not None and type(auto_on) not in (bool,int):return None
            if type(auto_on) is int and auto_on not in (0,1):return None
            normalized_plug['plug_auto_on']=None if auto_on is None else bool(auto_on)
            config['plug_configuration']=normalized_plug
        encode=lambda value:json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
        if _recipe:
            for name in ('active_materials','filament_slots','plug_configuration'):
                config.pop(name,None)
        label=b'ODIN recipe configuration' if _recipe else b'ODIN dispatch configuration'
        purpose_key=hmac.digest(key,label+b' key v1','sha256')
        return DispatchIdentity(hashlib.sha256(encode(target)).hexdigest(),
            hmac.digest(purpose_key,label+b' v1\x00'+encode(config),'sha256').hex())
    except Exception:
        return None


def stable_configuration_key(creds, key):
    """Recipe equality excludes transient slots and power controls entirely."""
    if not isinstance(creds, dict):
        return None
    stable = {name: value for name, value in creds.items()
              if name not in ('active_materials', 'filament_slots', 'plug_configuration')}
    identity = build_identity(stable, key, _recipe=True)
    return identity.configuration_key if identity else None


def receipt_configuration_matches(receipt, creds, key, full_configuration_key):
    """Bound-attempt equality; legacy receipts retain full dispatch equality.

    Never backfill here: only dispatch or audited backfill evidence may relax
    mutable loadout equality. Admission still requires the full key.
    """
    stable = receipt.get('stable_configuration_key')
    if stable is None:
        return receipt['configuration_key'] == full_configuration_key
    return (isinstance(stable, str) and re.fullmatch('[0-9a-f]{64}', stable) is not None
            and receipt.get('stable_key_source') in ('dispatch', 'backfilled_no_printer_audit')
            and stable == stable_configuration_key(creds, key))
