"""Quick Print is offered only for an upload the scheduler will accept.

A file-bound job needs the stored package's plate G-code (core.print_payload.validate_plate),
so an uploaded 3MF without Metadata/plate_1.gcode is refused at schedule time ("Selected
print plate is not present in the stored file"), even when its slice_info makes it look
sliced (is_sliced). The upload response now says whether Quick Print can queue it and why
not, from the same plate check."""
from tests.test_contracts.test_api_token_delegation_behavior import token_client  # noqa: F401
from tests.test_contracts.test_cert_C_models import _upload, library, sliced_3mf  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401


def test_a_3mf_without_plate_gcode_is_not_quick_printable_and_says_why(library):  # noqa: F811
    client, issue = library
    body = _upload(client, issue(['write:models']), sliced_3mf(plates=0))
    assert body["quick_print_ready"] is False
    assert "plate_1.gcode" in body["quick_print_blocked_reason"]
    refused = client.post(f"/models/{body['model_id']}/schedule", headers=issue(['write:jobs']))
    assert refused.status_code == 400 and "Selected print plate is not present" in refused.text


def test_a_sliced_3mf_is_quick_printable(library):  # noqa: F811
    client, issue = library
    body = _upload(client, issue(['write:models']), sliced_3mf(plates=1))
    assert body["quick_print_ready"] is True and body["quick_print_blocked_reason"] is None
