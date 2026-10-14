"""Closed manifest accepts history but never imports operational identities."""
import json
from copy import deepcopy
import pytest
from modules.archives.project_zip_manifest import parse_project_manifest,MAX_FILE_BYTES


def manifest():
    return {'version':2,'project':{'name':'Owned','description':None,'color':'#6366f1','expected_parts':0},
            'archives':[{'archive_key':'0','protection':'education','display':{'print_name':'Print','notes':None,'tags':[],
                'status':'completed','printed_at':'2026-10-13T12:00:00.123Z','actual_print_seconds':0,'filament_used_g':0},
                'file':{'member':'files/000.3mf','size':4,'sha256':'a'*64,'original_name':'same.3mf'}}]}


def parse(value):return parse_project_manifest(json.dumps(value).encode())


def test_closed_manifest_preserves_zero_history_and_protected_label():
    result=parse(manifest())
    assert result.project.expected_parts==0
    assert result.archives[0].display.actual_print_seconds==0
    assert result.archives[0].protection=='education'
    assert result.archives[0].file.member=='files/000.3mf'


def test_duplicate_basenames_have_distinct_members():
    value=manifest();second=deepcopy(value['archives'][0]);second['archive_key']='1';second['file']['member']='files/001.3mf'
    value['archives'].append(second)
    assert len(parse(value).archives)==2


@pytest.mark.parametrize('change',['version_float','operational_id','absolute','traversal','duplicate_member','bool_size','too_large','sha','control_name','invalid_time','nonfinite','long_tag','wrong_index','extra_root'])
def test_invalid_or_operational_metadata_refuses(change):
    value=manifest();archive=value['archives'][0];file=archive['file'];display=archive['display']
    if change=='version_float':value['version']=2.0
    elif change=='operational_id':archive['job_id']=1
    elif change=='absolute':file['member']='/files/000.3mf'
    elif change=='traversal':file['member']='files/../secret'
    elif change=='duplicate_member':
        second=deepcopy(archive);second['archive_key']='1';value['archives'].append(second)
    elif change=='bool_size':file['size']=True
    elif change=='too_large':file['size']=MAX_FILE_BYTES+1
    elif change=='sha':file['sha256']='A'*64
    elif change=='control_name':file['original_name']='bad\nname'
    elif change=='invalid_time':display['printed_at']='2026-02-30T12:00:00Z'
    elif change=='nonfinite':display['actual_print_seconds']=float('inf')
    elif change=='long_tag':display['tags']=['x'*65]
    elif change=='wrong_index':archive['archive_key']='00'
    elif change=='extra_root':value['org_id']=1
    with pytest.raises(ValueError):parse(value)


@pytest.mark.parametrize('payload',[b'{"version":2,"version":2}',b'['*9+b'0'+b']'*9,b'x'*(1024*1024+1),b'\xff'])
def test_duplicate_depth_size_and_invalid_utf8_refuse(payload):
    with pytest.raises(ValueError):parse_project_manifest(payload)


def test_declared_total_bound_applies_across_individually_valid_files():
    value=manifest();value['archives']=[]
    for index in range(5):
        archive=deepcopy(manifest()['archives'][0]);archive['archive_key']=str(index)
        archive['file'].update(member=f'files/{index:03d}.3mf',size=MAX_FILE_BYTES)
        value['archives'].append(archive)
    with pytest.raises(ValueError,match='total exceeds'):parse(value)


@pytest.mark.parametrize('count,accepted',[(128,True),(129,False)])
def test_archive_count_exact_boundary(count,accepted):
    value=manifest();value['archives']=[]
    for index in range(count):
        archive=deepcopy(manifest()['archives'][0]);archive.update(archive_key=str(index),file=None)
        value['archives'].append(archive)
    if accepted:assert len(parse(value).archives)==128
    else:
        with pytest.raises(ValueError):parse(value)


def test_metadata_exact_byte_boundary_and_one_byte_over():
    from modules.archives.project_zip_manifest import MAX_METADATA_BYTES
    raw=json.dumps(manifest()).encode()
    exact=raw+b' '*(MAX_METADATA_BYTES-len(raw))
    assert parse_project_manifest(exact).version==2
    with pytest.raises(ValueError,match='metadata size'):parse_project_manifest(exact+b' ')


def test_exact_perfile_and_aggregate_byte_boundaries():
    value=manifest();value['archives']=[]
    for index in range(4):
        archive=deepcopy(manifest()['archives'][0]);archive['archive_key']=str(index)
        archive['file'].update(member=f'files/{index:03d}.3mf',size=MAX_FILE_BYTES)
        value['archives'].append(archive)
    assert sum(archive.file.size for archive in parse(value).archives)==512*1024*1024
