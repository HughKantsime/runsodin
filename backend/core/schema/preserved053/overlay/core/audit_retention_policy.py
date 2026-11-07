"""Enterprise audit evidence survives at least ninety days; zero keeps forever."""
from fastapi import HTTPException


def effective_retention(config):
    from license_manager import get_license
    result = dict(config)
    if get_license().has_feature('audit_integrity') and 0 < result['audit_logs_days'] < 90:
        result['audit_logs_days'] = 90
    return result


def validate_retention(config):
    from license_manager import get_license
    if get_license().has_feature('audit_integrity') and 0 < config.get('audit_logs_days', 0) < 90:
        raise HTTPException(400, 'audit_logs_days must be 0 (forever) or at least 90 when audit integrity is licensed')
