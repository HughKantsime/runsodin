"""Validate inert branding artwork without fetching external resources."""
from io import BytesIO
import warnings
from defusedxml import ElementTree
from PIL import Image

MAX_PIXELS = 16_000_000
SVG_NS = 'http://www.w3.org/2000/svg'
TAGS = {'svg','g','path','rect','circle','ellipse','line','polyline','polygon','defs','linearGradient','radialGradient','stop','clipPath','mask','use','title','desc','text','tspan'}
ATTRS = {'id','viewBox','width','height','x','y','x1','x2','y1','y2','cx','cy','r','rx','ry','d','points','transform','fill','stroke','stroke-width','stroke-linecap','stroke-linejoin','stroke-miterlimit','stroke-dasharray','stroke-dashoffset','fill-rule','clip-rule','opacity','fill-opacity','stroke-opacity','offset','stop-color','stop-opacity','gradientUnits','gradientTransform','spreadMethod','fx','fy','fr','clipPathUnits','maskUnits','maskContentUnits','preserveAspectRatio','font-family','font-size','font-weight','text-anchor','dominant-baseline','clip-path','mask','href'}


def validated_image_type(content):
    import re
    head=content.lstrip()
    if head.startswith(b'<'):
        # XML declarations are permitted; other processing instructions are not.
        declaration=re.sub(br'^<\?xml\s[^?]*\?>',b'',head,count=1)
        if b'<?' in declaration or b'<!' in declaration:
            raise ValueError('Unsupported XML declarations')
        root=ElementTree.fromstring(content)
        if root.tag not in {'svg','{'+SVG_NS+'}svg'}:
            raise ValueError('Not SVG artwork')
        for node in root.iter():
            tag=node.tag
            if tag.startswith('{'):
                ns,tag=tag[1:].split('}',1)
                if ns!=SVG_NS:raise ValueError('Unsupported SVG namespace')
            if tag not in TAGS:raise ValueError('Unsupported SVG element')
            for name,value in node.attrib.items():
                if name.startswith('{'):
                    ns,name=name[1:].split('}',1)
                    if ns!='http://www.w3.org/1999/xlink' or name!='href':raise ValueError('Unsupported SVG attribute namespace')
                if name not in ATTRS or any(ord(c)<32 for c in value):raise ValueError('Unsupported SVG attribute')
                if "\\" in value:
                    raise ValueError('Unsupported SVG escape')
                if name in {'fill','stroke','stop-color'}:
                    if not re.fullmatch(r'(?:#[0-9a-fA-F]{3,8}|[A-Za-z]+|url\(#[A-Za-z_][\w.-]*\))',value):
                        raise ValueError('Unsupported SVG paint')
                if name in {'clip-path','mask'} and not re.fullmatch(r'(?:none|url\(#[A-Za-z_][\w.-]*\))',value):
                    raise ValueError('Unsupported SVG resource')
                if name=='href' and not re.fullmatch(r'#[A-Za-z_][\w.-]*',value):raise ValueError('External SVG reference')
                if 'url' in value.lower():
                    if name not in {'fill','stroke','clip-path','mask'} or not re.fullmatch(r'url\(#[A-Za-z_][\w.-]*\)',value):raise ValueError('Unsupported SVG resource')
        return 'svg'
    with warnings.catch_warnings():
        warnings.simplefilter('error',Image.DecompressionBombWarning)
        with Image.open(BytesIO(content)) as image:
            kind={'PNG':'png','JPEG':'jpeg','WEBP':'webp'}.get(image.format)
            if kind is None or image.width*image.height>MAX_PIXELS or getattr(image,'n_frames',1)!=1:raise ValueError('Unsupported image dimensions or format')
            image.verify()
        with Image.open(BytesIO(content)) as image:image.load()
    return kind


def store_asset(directory,kind,extension,content):
    """Publish a durable immutable file before selecting it in a DB transaction."""
    import os,secrets
    from pathlib import Path
    directory=Path(directory)
    # The deployment's static root is trusted; reject redirected child directories.
    if directory.is_symlink() or directory.parent.is_symlink():
        raise ValueError('Redirected branding directory')
    directory.mkdir(parents=True,exist_ok=True)
    flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
    directory_fd=os.open(directory,flags)
    try:
        filename=f'{kind}-{secrets.token_hex(16)}.{extension}'
        descriptor=os.open(filename,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory_fd)
        try:
            with os.fdopen(descriptor,'wb') as stream:
                stream.write(content);stream.flush();os.fsync(stream.fileno())
            os.fsync(directory_fd)
        except BaseException:
            os.unlink(filename,dir_fd=directory_fd)
            raise
        return filename
    finally:os.close(directory_fd)
