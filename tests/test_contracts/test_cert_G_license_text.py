"""Cert G: F20-20.2-08 parse the shipped LICENSE parameters and conversion clause."""
import re
from datetime import date
from pathlib import Path

LICENSE = Path(__file__).resolve().parents[2] / "LICENSE"


def _params():
    text = LICENSE.read_text()
    head, _, terms = text.partition("\nTerms\n")
    params = dict(re.findall(r"^([A-Z][A-Za-z ]+):\s+(.+)$", head, re.M))
    return text, params, " ".join(terms.split())


def test_license_is_bsl_1_1():
    text, _, _ = _params()
    assert text.splitlines()[0].strip() == "Business Source License 1.1"


def test_change_license_is_apache_2():
    _, params, _ = _params()
    assert params["Change License"].strip() == "Apache License, Version 2.0"


def test_change_date_is_2029_02_07():
    _, params, _ = _params()
    assert date.fromisoformat(params["Change Date"].strip()) == date(2029, 2, 7)


def test_four_years_per_version_whichever_first():
    _, _, terms = _params()
    assert ("Effective on the Change Date, or the fourth anniversary of the first publicly available distribution "
            "of a specific version of the Licensed Work under this License, whichever comes first, the Licensor hereby "
            "grants you rights under the terms of the Change License") in terms
    assert "This License applies separately for each version of the Licensed Work" in terms
