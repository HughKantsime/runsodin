"""Google OAuth/Classroom and Canvas LTI 1.3/NRPS. Tests replace request_json only."""
import base64
import hashlib
import json
import secrets
import time
from urllib.parse import quote, urlencode, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from core.errors import ErrorCode, OdinError
from core.itar import ItarOutboundBlocked
from core.webhook_utils import WebhookSSRFError
from core.outbound_http import pinned_async_request, OutboundResponseTooLarge

GOOGLE_AUTH = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN = 'https://oauth2.googleapis.com/token'
GOOGLE_INFO = 'https://openidconnect.googleapis.com/v1/userinfo'
GOOGLE_API = 'https://classroom.googleapis.com/v1'
SCOPES = ('openid email https://www.googleapis.com/auth/classroom.courses.readonly '
          'https://www.googleapis.com/auth/classroom.rosters.readonly '
          'https://www.googleapis.com/auth/classroom.profile.emails')
LTI = 'https://purl.imsglobal.org/spec/lti/claim/'
NRPS = 'https://purl.imsglobal.org/spec/lti-nrps/claim/namesroleservice'
NRPS_SCOPE = 'https://purl.imsglobal.org/spec/lti-nrps/scope/contextmembership.readonly'


def refuse(reason, status=409):
    raise OdinError(ErrorCode.validation_failed, reason, status=status, extra={'reason': reason})


def https_url(value):
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        refuse('roster_https_url_required', 422)
    return value.rstrip('/')


def same_origin(value, origin):
    https_url(value)
    if urlsplit(value).netloc.lower() != urlsplit(origin).netloc.lower():
        refuse('roster_untrusted_endpoint', 422)
    return value


async def request_json(method, url, *, data=None, headers=None):
    """Shared bounded transport; preserve roster SSRF policy and error reasons."""
    try:
        https_url(url)
        response = await pinned_async_request(
            method, url, timeout=15, max_response_bytes=2 * 1024 * 1024,
            public_only=True, data=data, headers=headers,
        )
        if not 200 <= response.status_code < 300:
            refuse('roster_provider_refused', 502)
        result = response.json()
        if not isinstance(result, dict):
            refuse('roster_response_invalid', 502)
        return result, response.links.get('next', {}).get('url')
    except OutboundResponseTooLarge:
        refuse('roster_response_too_large', 502)
    except (httpx.HTTPError, ValueError, WebhookSSRFError, ItarOutboundBlocked):
        refuse('roster_provider_unavailable', 502)


def authorization(config, callback, state, *, connect=False, login_hint=None, message_hint=None):
    verifier = secrets.token_urlsafe(48)
    nonce = secrets.token_urlsafe(32)
    params = dict(client_id=config['client_id'], redirect_uri=callback, response_type='code',
                  state=state, scope=SCOPES if connect else 'openid email')
    if config['provider'] == 'google':
        params.update(code_challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('='),
                      code_challenge_method='S256')
        if connect:
            params.update(access_type='offline', prompt='consent')
        endpoint = GOOGLE_AUTH
    else:
        params.update(response_type='id_token', response_mode='form_post', scope='openid',
                      prompt='none', nonce=nonce, login_hint=login_hint)
        if message_hint:
            params['lti_message_hint'] = message_hint
        endpoint = config['authorization_url']
    return endpoint + '?' + urlencode(params), {'verifier': verifier, 'nonce': nonce, 'callback': callback}


async def google_callback(config, pending, code):
    token, _ = await request_json('POST', GOOGLE_TOKEN, data={
        'grant_type': 'authorization_code', 'client_id': config['client_id'],
        'client_secret': config['client_secret'], 'code': code,
        'redirect_uri': pending['callback'], 'code_verifier': pending['verifier']})
    if not token.get('access_token'):
        refuse('roster_access_token_missing')
    profile, _ = await request_json('GET', GOOGLE_INFO, headers={'Authorization': 'Bearer ' + token['access_token']})
    if not isinstance(profile.get('sub'), str) or not profile['sub'] or profile.get('email_verified') is not True:
        refuse('roster_identity_unverified', 403)
    email = str(profile.get('email', '')).lower()
    if email.rsplit('@', 1)[-1] not in config['allowed_domains']:
        refuse('roster_domain_refused', 403)
    if pending.get('connect'):
        granted = set(token.get('scope', '').split())
        if 'https://www.googleapis.com/auth/userinfo.email' in granted:
            granted.add('email')
        if not token.get('refresh_token') or not set(SCOPES.split()) <= granted:
            refuse('roster_scopes_missing')
    return profile['sub'], token


async def google_roster(config, tokens, course_id):
    token, _ = await request_json('POST', GOOGLE_TOKEN, data={
        'grant_type': 'refresh_token', 'client_id': config['client_id'],
        'client_secret': config['client_secret'], 'refresh_token': tokens['refresh_token']})
    if not token.get('access_token'):
        refuse('roster_reconnect_required')
    headers = {'Authorization': 'Bearer ' + token['access_token']}
    async def pages(path, key):
        items, seen, page = [], set(), None
        for _ in range(100):
            url = GOOGLE_API + '/' + path + '?' + urlencode({'pageSize': 100, **({'pageToken': page} if page else {})})
            data, _ = await request_json('GET', url, headers=headers)
            batch = data.get(key, [])
            if not isinstance(batch, list) or any(not isinstance(v, dict) for v in batch):
                refuse('roster_response_invalid', 502)
            items.extend(batch)
            if len(items) > 10000:
                refuse('roster_limit_exceeded', 422)
            page = data.get('nextPageToken')
            if not page:
                return items
            if page in seen:
                break
            seen.add(page)
        refuse('roster_pagination_incomplete', 502)
    courses = await pages('courses', 'courses')
    if course_id is None:
        return [{'id': c['id'], 'title': c.get('name', c['id'])} for c in courses if c.get('courseState') == 'ACTIVE']
    course = next((c for c in courses if c['id'] == course_id and c.get('courseState') == 'ACTIVE'), None)
    if not course:
        refuse('roster_course_unavailable', 404)
    members = []
    for key, role in [('teachers', 'manager'), ('students', 'student')]:
        for item in await pages('courses/' + quote(course_id, safe='') + '/' + key, key):
            profile = item.get('profile', {})
            members.append({'subject': profile.get('id') or item.get('userId'),
                            'email': profile.get('emailAddress'), 'role': role})
    return {'id': course_id, 'title': course['name'], 'term': course.get('section', ''), 'members': members}


async def canvas_launch(config, pending, encoded):
    try:
        header = jwt.get_unverified_header(encoded)
        if header.get('alg') != 'RS256' or not header.get('kid'):
            refuse('roster_lti_signature_invalid', 403)
        jwks, _ = await request_json('GET', config['jwks_url'])
        keys = [k for k in jwks.get('keys', []) if k.get('kid') == header['kid'] and k.get('kty') == 'RSA'
                and k.get('use', 'sig') == 'sig' and k.get('alg', 'RS256') == 'RS256']
        if len(keys) != 1:
            refuse('roster_lti_signature_invalid', 403)
        claims = jwt.decode(encoded, jwt.PyJWK.from_dict(keys[0]).key, algorithms=['RS256'],
            issuer=config['issuer'], audience=config['client_id'], leeway=60,
            options={'require': ['iss', 'aud', 'sub', 'exp', 'iat', 'nonce']})
        if (claims['nonce'] != pending['nonce'] or claims.get(LTI + 'deployment_id') != config['deployment_id']
            or claims.get(LTI + 'version') != '1.3.0' or claims.get(LTI + 'message_type') != 'LtiResourceLinkRequest'
            or claims.get(LTI + 'target_link_uri') != pending['callback']
            or (isinstance(claims['aud'], list) and len(claims['aud']) > 1 and claims.get('azp') != config['client_id'])
            or time.time() - claims['iat'] > 600):
            refuse('roster_lti_claims_invalid', 403)
        context = claims[LTI + 'context']
        service = claims[NRPS]
        if '2.0' not in service['service_versions'] or not context.get('id') or not claims['sub']:
            refuse('roster_lti_context_invalid', 403)
        same_origin(service['context_memberships_url'], config['api_origin'])
        return claims, {'id': context['id'], 'title': context.get('title') or context['id'],
                        'term': '', 'memberships_url': service['context_memberships_url']}
    except (jwt.PyJWTError, ValueError, KeyError, TypeError):
        refuse('roster_lti_claims_invalid', 403)


async def canvas_roster(config, context):
    now = int(time.time())
    assertion = jwt.encode({'iss': config['client_id'], 'sub': config['client_id'],
        'aud': config['token_url'], 'iat': now, 'exp': now + 60, 'jti': secrets.token_urlsafe(32)},
        config['private_key'], algorithm='RS256', headers={'kid': config['key_id']})
    token, _ = await request_json('POST', config['token_url'], data={
        'grant_type': 'client_credentials', 'client_assertion_type': 'urn:ietf:params:oauth:client-assertion-type:jwt-bearer',
        'client_assertion': assertion, 'scope': NRPS_SCOPE})
    if not isinstance(token.get('access_token'), str):
        refuse('roster_access_token_missing')
    url, members, seen, skipped = context['memberships_url'], [], set(), []
    for _ in range(100):
        same_origin(url, config['api_origin'])
        if url in seen:
            break
        seen.add(url)
        data, next_url = await request_json('GET', url, headers={
            'Authorization': 'Bearer ' + token['access_token'],
            'Accept': 'application/vnd.ims.lti-nrps.v2.membershipcontainer+json'})
        if str(data.get('context', {}).get('id', '')) != context['id'] or not isinstance(data.get('members'), list):
            refuse('roster_context_mismatch', 502)
        for item in data['members']:
            if item.get('status') != 'Active':
                continue
            prefix = 'http://purl.imsglobal.org/vocab/lis/v2/membership'
            recognized = {
                'manager': {'Instructor', 'TeachingAssistant', 'Instructor#TeachingAssistant',
                            prefix + '#Instructor', prefix + '#TeachingAssistant',
                            prefix + '/Instructor#TeachingAssistant'},
                'student': {'Learner', prefix + '#Learner'},
            }
            matched = False
            for role, aliases in recognized.items():
                if any(r in aliases for r in item.get('roles', [])):
                    members.append({'subject': item.get('user_id'), 'email': item.get('email'), 'role': role})
                    matched = True
            if not matched:
                skipped.append({'subject': item.get('user_id'), 'reason': 'roster_role_unmatched'})
        if len(members) + len(skipped) > 10000:
            refuse('roster_limit_exceeded', 422)
        if not next_url:
            return {**context, 'members': members, 'skipped': skipped}
        url = next_url
    refuse('roster_pagination_incomplete', 502)


def public_jwk(config):
    key = serialization.load_pem_private_key(config['private_key'].encode(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
        refuse('roster_rsa_key_required', 422)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {**jwk, 'kid': config['key_id'], 'use': 'sig', 'alg': 'RS256'}
