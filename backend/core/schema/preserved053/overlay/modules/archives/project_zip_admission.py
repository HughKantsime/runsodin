"""Bounded immutable ZIP validation only; no DB/filesystem/printing effects."""
import hashlib,io,re,stat,struct,time,zipfile,zlib
from dataclasses import dataclass
from modules.archives.project_zip_manifest import (
    parse_project_manifest,MAX_METADATA_BYTES,MAX_TOTAL_BYTES,MAX_FILE_BYTES,MAX_ARCHIVES,
)

MAX_COMPRESSED_BYTES=256*1024*1024
CHUNK_BYTES=64*1024
VALIDATION_SECONDS=60


@dataclass(frozen=True)
class ValidatedProjectZip:
    manifest:object
    manifest_sha256:str
    package_sha256:str
    total_file_bytes:int


def _refuse():
    raise ValueError('Invalid or oversized project ZIP')



def _preflight_directory(payload,tick):
    """Classic single-disk ZIP only; bounded inventory before ZipInfo allocation."""
    tick()
    end=payload.rfind(b'PK\x05\x06',max(0,len(payload)-65557))
    if end<0 or end+22>len(payload):_refuse()
    signature,disk,central_disk,disk_count,count,size,offset,comment=struct.unpack_from('<4s4H2IH',payload,end)
    if (disk or central_disk or disk_count!=count or not 1<=count<=MAX_ARCHIVES+1
            or size>65536 or size<count*46 or offset+size!=end
            or end+22+comment!=len(payload) or count==65535 or offset==0xffffffff
            or size==0xffffffff or payload[max(0,end-20):end-16]==b'PK\x06\x07'):_refuse()
    position=offset
    for _ in range(count):
        tick()
        if position+46>end or payload[position:position+4]!=b'PK\x01\x02':_refuse()
        needed=struct.unpack_from('<H',payload,position+6)[0]
        name_size,extra_size,comment_size,disk_start=struct.unpack_from('<4H',payload,position+28)
        local=struct.unpack_from('<I',payload,position+42)[0]
        compressed,uncompressed=struct.unpack_from('<2I',payload,position+20)
        following=position+46+name_size+extra_size+comment_size
        if (needed>20 or disk_start or following>end or not 1<=name_size<=64 or local>=offset
                or local+30>offset or payload[local:local+4]!=b'PK\x03\x04'
                or compressed==0xffffffff or uncompressed==0xffffffff):_refuse()
        if struct.unpack_from('<H',payload,local+4)[0]>20:_refuse()
        extra=position+46+name_size
        extra_end=extra+extra_size
        while extra<extra_end:
            if extra+4>extra_end:_refuse()
            tag,length=struct.unpack_from('<2H',payload,extra)
            extra+=4
            if tag==1 or extra+length>extra_end:_refuse()
            extra+=length
        position=following
    if position!=end:_refuse()
    tick()

def validate_project_zip(payload,*,clock=time.monotonic):
    if type(payload) is not bytes or len(payload)>MAX_COMPRESSED_BYTES:_refuse()
    deadline=clock()+VALIDATION_SECONDS
    def tick():
        if clock()>deadline:_refuse()
    try:
        _preflight_directory(payload,tick)
        with zipfile.ZipFile(io.BytesIO(payload)) as package:
            members=package.infolist()
            if not 1<=len(members)<=MAX_ARCHIVES+1:_refuse()
            names=set();declared=0
            for member in members:
                tick()
                name=member.filename
                if name!=member.orig_filename or name in names or member.flag_bits&1 or member.is_dir() or member.external_attr&0x10:_refuse()
                names.add(name)
                if name!='project.json' and not re.fullmatch(r'files/[0-9]{3}\.(?:3mf|gcode|bgcode)',name):_refuse()
                mode=(member.external_attr>>16)&0xffff
                kind=stat.S_IFMT(mode)
                if kind not in (0,stat.S_IFREG):_refuse()
                if member.compress_type not in (zipfile.ZIP_STORED,zipfile.ZIP_DEFLATED):_refuse()
                if type(member.file_size) is not int or member.file_size<0:_refuse()
                bound=MAX_METADATA_BYTES if name=='project.json' else MAX_FILE_BYTES
                if member.file_size>bound:_refuse()
                declared+=member.file_size
            if 'project.json' not in names or declared>MAX_TOTAL_BYTES+MAX_METADATA_BYTES:_refuse()
            def stream(member,expected_size,expected_hash=None,metadata=False):
                total=0;digest=hashlib.sha256();parts=[]
                with package.open(member) as source:
                    while True:
                        tick()
                        chunk=source.read(min(CHUNK_BYTES,expected_size-total+1))
                        if not chunk:break
                        total+=len(chunk)
                        if total>expected_size:_refuse()
                        digest.update(chunk)
                        if metadata:parts.append(chunk)
                if total!=expected_size or expected_hash is not None and digest.hexdigest()!=expected_hash:_refuse()
                return b''.join(parts) if metadata else total
            metadata=package.getinfo('project.json')
            raw=stream(metadata,metadata.file_size,metadata=True)
            manifest=parse_project_manifest(raw)
            expected={'project.json'};total=0
            for archive in manifest.archives:
                file=archive.file
                if file is None:continue
                expected.add(file.member)
                if file.member not in names:_refuse()
                member=package.getinfo(file.member)
                if member.file_size!=file.size:_refuse()
                total+=stream(member,file.size,file.sha256)
                if total>MAX_TOTAL_BYTES:_refuse()
            if names!=expected:_refuse()
            manifest_hash=hashlib.sha256(raw).hexdigest();package_hash=hashlib.sha256(payload).hexdigest()
            tick()
            return ValidatedProjectZip(manifest,manifest_hash,package_hash,total)
    except (zipfile.BadZipFile,RuntimeError,NotImplementedError,EOFError,OSError,UnicodeError,zlib.error,struct.error):
        raise ValueError('Invalid or oversized project ZIP') from None
