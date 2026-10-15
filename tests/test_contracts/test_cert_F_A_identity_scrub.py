"""Security review MED/LOW-E: the GDPR identity scrub matches on token boundaries,
is case-insensitive for email, handles @mentions and punctuation, scrubs values only
in identifier-bearing keys, leaves role-like keys and bystanders intact."""
import json
import pytest
from core.identity_scrub import scrub_text, scrub_json_text

IDS = ["bob", "Bob@Example.Test"]


@pytest.mark.parametrize("text_in,expected", [
    ("bob logged in", "[deleted-user] logged in"),
    ("@bob mentioned you", "@[deleted-user] mentioned you"),
    ("from bob.", "from [deleted-user]."),
    ("(bob)", "([deleted-user])"),
    ("bobby stayed", "bobby stayed"),            # longer token not matched
    ("robob", "robob"),                          # substring not matched
    ("mail BOB@example.test now", "mail [deleted-user] now"),  # email case-insensitive
])
def test_bounded_and_caseinsensitive_text(text_in, expected):
    assert scrub_text(text_in, IDS) == expected


def test_json_identifier_keys_exact_only_text_keys_bounded_protected_untouched():
    raw = json.dumps({
        "username": "bob",                 # identifier key, exact -> scrubbed
        "new_role": "bob",                 # protected key -> kept even though it equals an id
        "role": "admin",
        "message": "bob approved the job",  # free text -> bounded scrub
        "note": "see bobby instead",        # bystander name -> untouched
        "actor": "Bob@Example.Test",        # identifier key, email case-insensitive
        "count": 3,
    })
    out = json.loads(scrub_json_text(raw, IDS))
    assert out["username"] == "[deleted-user]"
    assert out["new_role"] == "bob"
    assert out["role"] == "admin"
    assert out["message"] == "[deleted-user] approved the job"
    assert out["note"] == "see bobby instead"
    assert out["actor"] == "[deleted-user]"
    assert out["count"] == 3


def test_non_matching_row_returns_none():
    assert scrub_json_text(json.dumps({"username": "carol", "message": "carol did it"}), IDS) is None


def test_non_ascii_identifier_in_escaped_json():
    raw = json.dumps({"username": "zoë", "message": "zoë signed in"})  # ensure_ascii escapes
    out = json.loads(scrub_json_text(raw, ["zoë"]))
    assert out == {"username": "[deleted-user]", "message": "[deleted-user] signed in"}
