"""Owned local uploads must validate image content and reject active SVG content."""
from io import BytesIO
from PIL import Image
import pytest
from tests.test_contracts.test_branding_behavior import branding_client,review_db

# Exercises Pro/Enterprise-gated routes (cert-fix-G tier gates): run under a real signed license.
pytestmark = pytest.mark.usefixtures("pro_license")

@pytest.fixture
def owned_branding(branding_client,tmp_path,monkeypatch):
    from modules.system import routes_settings
    root=tmp_path/'modules'
    monkeypatch.setattr(routes_settings,'__file__',str(root/'system'/'routes_settings.py'))
    return branding_client,root.parent/'static'/'branding'

@pytest.mark.parametrize('path',['/branding/logo','/branding/favicon'])
@pytest.mark.parametrize('payload',[b'\x89PNGnot a real image',b'<?xml version="1.0"?><not-an-image/>',b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>',b'<svg xmlns="http://www.w3.org/2000/svg"><image href="https://external.example.invalid/image"/></svg>'])
def test_invalid_or_active_branding_image_rejected_without_file_or_db_mutation(owned_branding,path,payload):
    client,directory=owned_branding
    before=client.get('/branding').json()
    response=client.post(path,files={'file':('synthetic-image.png',payload,'image/png')})
    assert response.status_code==400,response.text
    assert client.get('/branding').json()==before
    assert not directory.exists() or not list(directory.iterdir())


def test_valid_png_upload_and_remove_are_persistent(owned_branding):
    client,directory=owned_branding
    image=BytesIO();Image.new('RGB',(16,16),(20,80,120)).save(image,format='PNG')
    response=client.post('/branding/logo',files={'file':('untrusted-filename.jpg',image.getvalue(),'image/jpeg')})
    assert response.status_code==200,response.text
    assert response.json()['logo_url'].startswith('/static/branding/logo-') and response.json()['logo_url'].endswith('.png')
    assert (directory/response.json()['logo_url'].rsplit('/',1)[1]).read_bytes()==image.getvalue()
    assert client.get('/branding').json()['logo_url']==response.json()['logo_url']
    assert client.delete('/branding/logo').status_code==200
    assert client.get('/branding').json()['logo_url'] is None
    assert (directory/response.json()['logo_url'].rsplit('/',1)[1]).exists()  # logical removal retains bytes


@pytest.mark.parametrize('data_backed',[False,True])
def test_uploaded_logo_is_reachable_through_real_static_mount(branding_client,tmp_path,monkeypatch,data_backed):
    from fastapi.staticfiles import StaticFiles
    from modules.system import routes_settings
    backend=tmp_path/'backend'
    public=backend/'static';public.mkdir(parents=True)
    if data_backed:
        data=tmp_path/'data'/'branding';data.mkdir(parents=True)
        (public/'branding').symlink_to(data,target_is_directory=True)
        monkeypatch.setenv('ODIN_BRANDING_DIRECTORY',str(data))
    monkeypatch.setattr(routes_settings,'__file__',str(backend/'modules'/'system'/'routes_settings.py'))
    from core.app import _mount_static_assets
    _mount_static_assets(branding_client.app,public)
    image=BytesIO();Image.new('RGB',(16,16),(20,80,120)).save(image,format='PNG')
    response=branding_client.post('/branding/logo',files={'file':('synthetic.png',image.getvalue(),'image/png')})
    assert response.status_code==200,response.text
    served=branding_client.get(response.json()['logo_url'])
    assert served.status_code==200
    assert served.content==image.getvalue()

@pytest.mark.parametrize('kind,extension',[('JPEG','jpg'),('WEBP','webp'),('PNG','png')])
def test_supported_raster_formats_preserve_exact_bytes(owned_branding,kind,extension):
    client,directory=owned_branding
    image=BytesIO();Image.new('RGB',(12,8),(30,50,70)).save(image,format=kind)
    response=client.post('/branding/favicon',files={'file':('synthetic.bin',image.getvalue(),'application/octet-stream')})
    assert response.status_code==200,response.text
    assert response.json()['favicon_url'].startswith('/static/branding/favicon-') and response.json()['favicon_url'].endswith('.'+extension)
    assert (directory/response.json()['favicon_url'].rsplit('/',1)[1]).read_bytes()==image.getvalue()


def test_inert_svg_groups_gradients_and_local_references_supported(owned_branding):
    client,directory=owned_branding
    content=b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20"><defs><linearGradient id="paint"><stop offset="0" stop-color="#123abc"/></linearGradient><path id="shape" d="M0 0L10 10"/></defs><g transform="translate(1 1)"><use href="#shape" fill="url(#paint)"/></g></svg>'
    response=client.post('/branding/logo',files={'file':('synthetic.svg',content,'image/svg+xml')})
    assert response.status_code==200,response.text
    assert (directory/response.json()['logo_url'].rsplit('/',1)[1]).read_bytes()==content

@pytest.mark.parametrize('value',[r'u\72l(https://example.invalid/paint#x)',r'\75rl(https://example.invalid/paint#x)','var(--external-paint)'])
def test_svg_presentation_functions_cannot_hide_external_resources(owned_branding,value):
    client,directory=owned_branding
    content=f'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0L1 1" fill="{value}"/></svg>'.encode()
    response=client.post('/branding/logo',files={'file':('synthetic.svg',content,'image/svg+xml')})
    assert response.status_code==400
    assert not directory.exists()


def _png(color):
    stream=BytesIO();Image.new('RGB',(10,10),color).save(stream,format='PNG');return stream.getvalue()


def test_failed_database_save_preserves_previously_usable_logo(owned_branding,monkeypatch):
    from sqlalchemy.orm import Session
    client,directory=owned_branding
    old=_png((10,20,30));new=_png((40,50,60))
    assert client.post('/branding/logo',files={'file':('old.png',old,'image/png')}).status_code==200
    before=client.get('/branding').json()
    def fail_commit(session):raise RuntimeError('synthetic database failure')
    monkeypatch.setattr(Session,'commit',fail_commit)
    with pytest.raises(RuntimeError,match='synthetic database failure'):
        client.post('/branding/logo',files={'file':('new.png',new,'image/png')})
    assert client.get('/branding').json()==before
    assert (directory/before['logo_url'].rsplit('/',1)[1]).read_bytes()==old


def test_upload_never_follows_existing_file_symlink(owned_branding,tmp_path):
    client,directory=owned_branding
    directory.mkdir(parents=True)
    outside=tmp_path/'outside-evidence';outside.write_bytes(b'preserve original evidence')
    (directory/'logo.png').symlink_to(outside)
    response=client.post('/branding/logo',files={'file':('synthetic.png',_png((30,40,50)),'image/png')})
    assert response.status_code in {200,400}
    assert outside.read_bytes()==b'preserve original evidence'


def test_failed_logo_removal_preserves_database_pointer_and_asset(owned_branding,monkeypatch):
    from sqlalchemy.orm import Session
    client,directory=owned_branding
    image=_png((20,40,60))
    assert client.post('/branding/logo',files={'file':('synthetic.png',image,'image/png')}).status_code==200
    before=client.get('/branding').json()
    def fail_commit(session):raise RuntimeError('synthetic database failure')
    monkeypatch.setattr(Session,'commit',fail_commit)
    with pytest.raises(RuntimeError,match='synthetic database failure'):
        client.delete('/branding/logo')
    assert client.get('/branding').json()==before
    assert (directory/before['logo_url'].rsplit('/',1)[1]).read_bytes()==image


def test_uncertain_commit_keeps_newly_selected_asset_readable(owned_branding,monkeypatch):
    from sqlalchemy.orm import Session
    client,directory=owned_branding
    client.get('/branding')
    original=Session.commit
    def committed_then_failed(session):
        original(session)
        raise RuntimeError('synthetic uncertain commit')
    monkeypatch.setattr(Session,'commit',committed_then_failed)
    content=_png((80,90,100))
    with pytest.raises(RuntimeError,match='synthetic uncertain commit'):
        client.post('/branding/logo',files={'file':('synthetic.png',content,'image/png')})
    selected=client.get('/branding').json()['logo_url']
    assert selected is not None
    assert (directory/selected.rsplit('/',1)[1]).read_bytes()==content


def test_concurrent_asset_writes_create_distinct_complete_files(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from modules.system.branding_assets import store_asset
    directory=tmp_path/'static'/'branding'
    content=[_png((index,20,30)) for index in range(8)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        names=list(pool.map(lambda data:store_asset(directory,'logo','png',data),content))
    assert len(set(names))==len(content)
    assert all((directory/name).read_bytes()==data for name,data in zip(names,content))


def test_uploaded_favicon_can_be_removed_through_ui_endpoint(owned_branding):
    client,directory=owned_branding
    uploaded=client.post('/branding/favicon',files={'file':('synthetic.png',_png((40,80,100)),'image/png')})
    assert uploaded.status_code==200
    response=client.delete('/branding/favicon')
    assert response.status_code==200,response.text
    assert response.json()=={'favicon_url':None}
    assert client.get('/branding').json()['favicon_url'] is None
    assert (directory/uploaded.json()['favicon_url'].rsplit('/',1)[1]).exists()
