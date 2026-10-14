"""Actual detail header/DELETE preconditions; no retirement syscall is invoked."""
from tests.test_contracts.test_timelapse_route_activation import activated,prepared_source,encoder,frame_writer,review_db,owned_runtime

def test_authorized_detail_exposes_exact_nonce_etag_and_precondition_refusals(activated):
 client,engine,root,tid,nonce,token=activated;headers={'Authorization':'Bearer '+token}
 detail=client.get(f'/timelapses/{tid}',headers=headers)
 assert detail.status_code==200
 assert detail.headers['etag']=='"'+nonce+'"'
 assert detail.json()['id']==tid
 assert client.delete(f'/timelapses/{tid}',headers=headers).status_code==428
 assert client.delete(f'/timelapses/{tid}',headers={**headers,'If-Match':'17'}).status_code==400
 assert client.delete(f'/timelapses/{tid}',headers={**headers,'If-Match':'"stale"'}).status_code==412
 assert list(root.glob('encode_*.mp4'))
