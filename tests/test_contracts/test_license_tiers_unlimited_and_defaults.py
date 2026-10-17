"""Final-review fixes O13 and O14 (spec .claude/specs/review-fixes-2.0.md).

* O13 (Hugh): Education and Enterprise are unlimited, like Pro.
* O14: a portal default key (features null) gets the tier defaults,
  including education_workflows; explicit lists stay additive.
"""
import pytest


# ── O13 / O14 ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tier", ["education", "enterprise"])
def test_education_and_enterprise_are_unlimited(tier_license, tier):
    import license_manager as lm
    assert lm.TIERS[tier]["max_printers"] is None and lm.TIERS[tier]["max_users"] is None
    info = tier_license(tier)
    assert info.max_printers is None and info.max_users is None
    lm.check_printer_limit(100000)
    lm.check_user_limit(100000)


@pytest.mark.parametrize("tier", ["education", "enterprise"])
def test_a_portal_default_key_gets_the_tier_defaults_including_education_workflows(tier_license, tier):
    # The portal signs "features": null when the admin leaves the list blank.
    info = tier_license(tier, features=None, max_printers=None, max_users=None)
    assert info.has_feature("education_workflows") is True
    assert "education_workflows" in info.effective_features()
    assert {"job_approval", "user_groups", "print_quotas"} <= set(info.effective_features())


def test_explicit_feature_lists_stay_additive_and_pro_never_gets_education_workflows(tier_license):
    info = tier_license("education", features=["custom_school_feature"])
    assert {"custom_school_feature", "job_approval"} <= set(info.effective_features())
    assert info.has_feature("education_workflows") is False  # an explicit list must name it
    assert tier_license("pro", features=None).has_feature("education_workflows") is False
