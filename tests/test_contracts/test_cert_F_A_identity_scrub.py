"""Security review round-4 LOW/MEDIUM: the GDPR identity scrub matches on identifier
edges (word chars and . + - @ do not split an identifier), is case-insensitive for
email, scans every non-protected string value, renames identifier keys, and never
rewrites enum-like values (role/new_role/status)."""
import json
import pytest
from core.identity_scrub import scrub_text, scrub_json_text

IDS = ["bob", "Bob@Example.Test"]


@pytest.mark.parametrize("text_in,expected", [
    ("bob logged in", "[deleted-user] logged in"),
    ("(bob)", "([deleted-user])"),
    ("bob, alice signed in", "[deleted-user], alice signed in"),
    ("a@x.com, Bob@Example.Test here", "a@x.com, [deleted-user] here"),   # email list
    # Identifier edges: . + - @ and word chars never split an identifier.
    ("bob-smith stayed", "bob-smith stayed"),
    ("alice-bob stayed", "alice-bob stayed"),
    ("bob+test stayed", "bob+test stayed"),
    ("bob@x.com.au stayed", "bob@x.com.au stayed"),
    ("@bob stayed", "@bob stayed"),
    ("from bob.smith", "from bob.smith"),
    ("bobby stayed", "bobby stayed"),
    ("robob", "robob"),
    ("mail BOB@example.test now", "mail [deleted-user] now"),  # email case-insensitive
])
def test_identifier_edges_and_caseinsensitive_text(text_in, expected):
    assert scrub_text(text_in, IDS) == expected


def test_json_scans_all_values_renames_id_keys_and_keeps_enum_values():
    raw = json.dumps({
        "username": "bob",                   # value scrubbed
        "new_role": "bob",                   # protected -> kept even though it equals an id
        "role": "admin",
        "status": "bob",                     # protected -> kept
        "message": "bob approved the job",   # free text -> bounded scrub
        "note": "see bobby instead",         # bystander -> untouched
        "actor": "Bob@Example.Test",         # email, case-insensitive
        "owner": "bob-smith",                # edge-protected -> kept
        "bob": "did it",                     # key is an identifier -> key renamed
        "count": 3,
    })
    out = json.loads(scrub_json_text(raw, IDS))
    assert out["username"] == "[deleted-user]"
    assert out["new_role"] == "bob"
    assert out["role"] == "admin"
    assert out["status"] == "bob"
    assert out["message"] == "[deleted-user] approved the job"
    assert out["note"] == "see bobby instead"
    assert out["actor"] == "[deleted-user]"
    assert out["owner"] == "bob-smith"
    assert out["[deleted-user]"] == "did it" and "bob" not in out
    assert out["count"] == 3
    assert out["count"] == 3


def test_non_matching_row_returns_none():
    assert scrub_json_text(json.dumps({"username": "carol", "message": "carol did it"}), IDS) is None


def test_non_ascii_identifier_in_escaped_json():
    raw = json.dumps({"username": "zoë", "message": "zoë signed in"})  # ensure_ascii escapes
    out = json.loads(scrub_json_text(raw, ["zoë"]))
    assert out == {"username": "[deleted-user]", "message": "[deleted-user] signed in"}
