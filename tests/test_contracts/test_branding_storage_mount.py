"""Public branding stays bound to its explicit directory and opened bytes."""
from pathlib import Path
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.branding_storage import BrandingAssets,branding_directory


@pytest.fixture
def mount(tmp_path):
    root=tmp_path/'data'/'branding';root.mkdir(parents=True)
    assets=BrandingAssets(root)
    app=FastAPI();app.mount('/static/branding',assets)
    try:
        yield TestClient(app),root,assets
    finally:
        assets.close()


def test_get_head_cache_and_method_keep_exact_bytes(mount):
    client,root,_=mount
    (root/'logo.png').write_bytes(b'public branding')
    response=client.get('/static/branding/logo.png')
    assert response.status_code==200 and response.content==b'public branding'
    assert response.headers['content-type']=='image/png'
    assert client.head('/static/branding/logo.png').content==b''
    assert client.head('/static/branding/logo.png').headers['content-length']=='15'
    assert client.get('/static/branding/logo.png',headers={'If-None-Match':response.headers['etag']}).status_code==304
    assert client.post('/static/branding/logo.png').status_code==405


@pytest.mark.parametrize('kind',['symlink','hardlink','fifo','directory','oversize'])
def test_unsafe_leaf_never_serves_private_bytes(mount,tmp_path,kind):
    client,root,_=mount
    private=tmp_path/'private';private.write_bytes(b'private must not serve')
    leaf=root/'logo.png'
    if kind=='symlink':leaf.symlink_to(private)
    elif kind=='hardlink':os.link(private,leaf)
    elif kind=='fifo':os.mkfifo(leaf)
    elif kind=='directory':leaf.mkdir()
    else:
        with leaf.open('wb') as stream:stream.truncate(16*1024*1024+1)
    response=client.get('/static/branding/logo.png')
    assert response.status_code==404 and b'private' not in response.content


@pytest.mark.parametrize('replacement',['directory','symlink','parent'])
def test_root_or_parent_replacement_cannot_redirect_mount(mount,tmp_path,replacement):
    client,root,_=mount
    (root/'logo.png').write_bytes(b'public')
    if replacement=='parent':
        root.parent.rename(tmp_path/'old-data')
        root.parent.mkdir()
        root.mkdir()
    else:
        root.rename(tmp_path/'old-branding')
        if replacement=='symlink':
            private=tmp_path/'private';private.mkdir()
            (private/'logo.png').write_bytes(b'private')
            root.symlink_to(private)
        else:root.mkdir()
    if replacement!='symlink':(root/'logo.png').write_bytes(b'private')
    assert client.get('/static/branding/logo.png').status_code==404


def test_symlink_parent_refused_before_creating_children(tmp_path):
    outside=tmp_path/'outside';outside.mkdir()
    alias=tmp_path/'alias';alias.symlink_to(outside)
    with pytest.raises(OSError):BrandingAssets(alias/'branding')
    assert not (outside/'branding').exists()


def test_deployment_configuration_requires_absolute_nontraversing_path(monkeypatch,tmp_path):
    monkeypatch.delenv('ODIN_BRANDING_DIRECTORY',raising=False)
    assert branding_directory(tmp_path/'branding')==tmp_path/'branding'
    for value in ('relative/branding','/data/../private'):
        monkeypatch.setenv('ODIN_BRANDING_DIRECTORY',value)
        with pytest.raises(ValueError):branding_directory(tmp_path/'branding')


def test_general_static_precedence_and_leaf_traversal(tmp_path,monkeypatch):
    from core.app import _mount_static_assets
    public=tmp_path/'static';public.mkdir()
    (public/'ordinary.txt').write_bytes(b'ordinary')
    directory=tmp_path/'data'/'branding'
    monkeypatch.setenv('ODIN_BRANDING_DIRECTORY',str(directory))
    app=FastAPI();_mount_static_assets(app,public)
    try:
        (directory/'logo.png').write_bytes(b'public')
        client=TestClient(app)
        assert client.get('/static/ordinary.txt').content==b'ordinary'
        assert client.get('/static/branding/logo.png').content==b'public'
        assert client.get('/static/branding/%2e%2e/ordinary.txt').status_code==404
    finally:app.state.branding_assets.close()


def test_one_close_failure_does_not_leave_other_owned_descriptors(monkeypatch):
    from core import branding_storage
    seen=[]
    def close(descriptor):
        seen.append(descriptor)
        if descriptor==2:raise OSError('owned test close failure')
    monkeypatch.setattr(branding_storage.os,'close',close)
    branding_storage._close([1,2,3])
    assert seen==[3,2,1]
