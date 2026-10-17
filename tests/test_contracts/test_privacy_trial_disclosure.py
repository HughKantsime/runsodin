"""O16: the privacy policy discloses the Pro trial data (spec review-fixes-2.0.md)."""
import os
import re
from pathlib import Path

POLICY = Path(os.environ.get("ODIN_PRIVACY_POLICY", Path(__file__).resolve().parents[2] / "PRIVACY_POLICY.md"))


def _trial_section():
    text = POLICY.read_text()
    m = re.search(r"### 3\.\d+ Pro Trial Requests\n(.*?)(?=\n### |\n---)", text, re.S)
    assert m, "the policy has a Pro trial section"
    return text, m.group(1)


def test_trial_section_lists_every_data_item_and_the_processor():
    _, sec = _trial_section()
    for phrase in ("Email address", "cleartext on the pending request", "on the trial license key",
                   "one-way hash", "one trial per email address", "Installation ID", "device public key", "fingerprint",
                   "IP address", "48 hours", "90 days", "Resend"):
        assert phrase in sec, phrase


def test_retention_table_has_the_trial_rows():
    text, _ = _trial_section()
    assert "| Pending Pro trial requests" in text and "| No more than 48 hours |" in text
    assert "| IP address on a Pro trial record | 90 days |" in text
