"""Closed v2 project ZIP metadata only; grants no storage/dispatch authority."""
import json,re
from datetime import datetime
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,StrictInt,field_validator,model_validator

MAX_METADATA_BYTES=1024*1024
MAX_TOTAL_BYTES=512*1024*1024
MAX_FILE_BYTES=128*1024*1024
MAX_ARCHIVES=128


class _Closed(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,allow_inf_nan=False)


class ProjectDisplay(_Closed):
    name:str=Field(min_length=1,max_length=200)
    description:str|None=Field(max_length=10000)
    color:str=Field(pattern=r'^#[0-9A-Fa-f]{6}$')
    expected_parts:StrictInt|None=Field(ge=0,le=1000000)


class ArchiveDisplay(_Closed):
    print_name:str=Field(max_length=255)
    notes:str|None=Field(max_length=10000)
    tags:list[str]=Field(max_length=32)
    status:Literal['completed','failed','cancelled']
    printed_at:str|None=Field(max_length=27)
    actual_print_seconds:float|None=Field(ge=0,le=31536000)
    filament_used_g:float|None=Field(ge=0,le=1000000)

    @field_validator('tags')
    @classmethod
    def bounded_tags(cls,tags):
        if any(len(tag)>64 for tag in tags):raise ValueError('Invalid imported tag')
        return tags

    @field_validator('printed_at')
    @classmethod
    def utc_only(cls,value):
        if value is not None:
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3}(?:\d{3})?)?Z',value):
                raise ValueError('Invalid imported UTC time')
            datetime.fromisoformat(value)
        return value


class ArchiveFile(_Closed):
    member:str=Field(pattern=r'^files/[0-9]{3}\.(?:3mf|gcode|bgcode)$')
    size:StrictInt=Field(ge=0,le=MAX_FILE_BYTES)
    sha256:str=Field(pattern=r'^[0-9a-f]{64}$')
    original_name:str=Field(min_length=1,max_length=255)

    @field_validator('original_name')
    @classmethod
    def safe_basename(cls,value):
        if value in ('.','..') or '/' in value or '\\' in value or any(ord(char)<32 or ord(char)==127 for char in value):
            raise ValueError('Invalid imported filename')
        return value


class ImportedArchive(_Closed):
    archive_key:str
    display:ArchiveDisplay
    protection:Literal['generic','education']
    file:ArchiveFile|None


class ProjectZipManifest(_Closed):
    version:Literal[2]
    project:ProjectDisplay
    archives:list[ImportedArchive]=Field(max_length=MAX_ARCHIVES)

    @field_validator('version',mode='before')
    @classmethod
    def strict_version(cls,value):
        if type(value) is not int or value!=2:raise ValueError('Invalid project ZIP version')
        return value

    @model_validator(mode='after')
    def exact_members(self):
        total=0
        for index,archive in enumerate(self.archives):
            if archive.archive_key!=str(index):raise ValueError('Invalid archive index')
            if archive.file is not None:
                prefix=f'files/{index:03d}.'
                if not archive.file.member.startswith(prefix):raise ValueError('Archive member differs')
                total+=archive.file.size
        if total>MAX_TOTAL_BYTES:raise ValueError('Project ZIP total exceeds bound')
        return self


def _pairs(pairs):
    if len(pairs)>32 or len({key for key,_ in pairs})!=len(pairs):
        raise ValueError('Invalid project ZIP metadata keys')
    return dict(pairs)


def _nonfinite(value):
    raise ValueError('Invalid project ZIP numeric value')


def parse_project_manifest(payload):
    if type(payload) is not bytes or len(payload)>MAX_METADATA_BYTES:
        raise ValueError('Invalid project ZIP metadata size')
    value=payload.decode('utf-8',errors='strict')
    quoted=escaped=False;depth=0
    for char in value:
        if quoted:
            if escaped:escaped=False
            elif char=='\\':escaped=True
            elif char=='"':quoted=False
        elif char=='"':quoted=True
        elif char in '[{':
            depth+=1
            if depth>8:raise ValueError('Invalid project ZIP metadata depth')
        elif char in ']}':depth-=1
    decoded=json.loads(value,object_pairs_hook=_pairs,parse_constant=_nonfinite)
    return ProjectZipManifest.model_validate(decoded)
