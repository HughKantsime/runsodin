"""Review fix O15: no compliance claim ODIN cannot certify, and no Enterprise price range.

FEATURES 12.4 names the GDPR data rights it implements; the FERPA/COPPA/VPAT files are
guidance documents, not compliance; TERMS quotes Enterprise pricing per agreement."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _text(name):
    return (ROOT / name).read_text()


def test_features_names_gdpr_data_rights_not_compliance():
    features = _text("FEATURES.md")
    assert "### 12.4 GDPR data rights (export and anonymization)" in features
    assert "GDPR Compliance" not in features
    assert "FERPA/COPPA/VPAT compliance docs" not in features


def test_roadmap_and_changelog_call_the_files_guidance_documents():
    for name in ("ROADMAP.md", "CHANGELOG.md"):
        text = _text(name)
        assert "Compliance docs" not in text, name
        assert "FERPA/COPPA/VPAT guidance documents" in text, name
    for doc in ("FERPA_COMPLIANCE.md", "COPPA_COMPLIANCE.md", "VPAT.md"):
        assert (ROOT / doc).exists(), doc  # the documents the wording refers to


def test_terms_quote_enterprise_pricing_per_agreement():
    terms = _text("TERMS_OF_SERVICE.md")
    assert "$2,500" not in terms and "$5,000" not in terms
    assert "| Enterprise | Quoted per agreement (contact sales) |" in terms
