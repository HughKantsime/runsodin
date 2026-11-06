"""Locally rendered certificate and domain-separated Ed25519 PDF digest signature."""
import base64
import hashlib
import json
import re
from pathlib import Path
from fastapi.encoders import jsonable_encoder
from fpdf import FPDF
from fastapi import HTTPException
from core.traceability import one, decoded


class CertificatePDF(FPDF):
    def footer(self):
        self.set_y(-12)
        self.set_font('Noto', '', 8)
        self.cell(0, 5, f'ODIN manufacturing record | Page {self.page_no()}/{{nb}}', align='C')

    def normalize_text(self, text):
        text = ''.join(c if c == '\n' or ord(c) >= 32 else ' ' for c in str(text))
        supported = set(self.current_font.cmap)
        for font in self.fonts.values():
            if hasattr(font, 'cmap'):
                supported.update(font.cmap)
        text = ''.join(c if c == '\n' or ord(c) in supported else '[?]' for c in text)
        return super().normalize_text(text)


SIGNING_DOMAIN = b'odin-traceability-certificate-v1\n'


def existing_key():
    """Read only: certificate export must never mint an installation identity."""
    import license_manager
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    try:
        path = Path(license_manager.LICENSE_DIR) / license_manager.DEVICE_PRIVATE_KEY_FILENAME
        private = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(private, Ed25519PrivateKey):
            raise ValueError('Not an Ed25519 device key')
    except (OSError, ValueError, TypeError):
        raise HTTPException(503, 'Existing installation device signing key unavailable; configure the installation key before exporting certificates') from None
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return private, public


def signing_identity():
    private, public = existing_key()
    return private, public, hashlib.sha256(public).hexdigest()


def signed(data, identity=None):
    private, public, fingerprint = identity or signing_identity()
    digest = hashlib.sha256(data).digest()
    return {'algorithm':'Ed25519','public_key':base64.b64encode(public).decode(),
            'key_fingerprint':fingerprint, 'signature':base64.b64encode(private.sign(SIGNING_DOMAIN + digest)).decode(),
            'sha256':digest.hex(),'signed_content':'odin-traceability-certificate-v1\n + raw SHA-256 digest of PDF'}


def certificate_branding(db, org_id):
    from modules.organizations.branding import branding_to_dict
    from modules.organizations.branding import Branding
    # Reading a certificate never creates branding or takes a write lock.
    branding_row = db.query(Branding).first()
    branding = branding_to_dict(branding_row) if branding_row else {}
    if org_id:
        org = one(db, 'SELECT name,settings_json FROM groups WHERE id=:id', id=org_id) or {}
        settings = decoded(org.get('settings_json'), {})
        branding['app_name'] = settings.get('branding_app_name') or org.get('name') or branding.get('app_name')
        branding['logo_url'] = settings.get('branding_logo_url') or branding.get('logo_url')
    return branding


def certificate(db, record, org_id, *, branding=None, identity=None):
    branding = certificate_branding(db, org_id) if branding is None else branding
    identity = identity or signing_identity()
    pdf = CertificatePDF()
    from datetime import datetime, timezone
    captured = datetime.fromisoformat(str(record['captured_at']).replace('Z', '+00:00'))
    pdf.set_creation_date(captured.replace(tzinfo=timezone.utc) if captured.tzinfo is None else captured)
    pdf.alias_nb_pages()
    fonts = Path(__file__).resolve().parents[1] / 'orders' / 'fonts'
    pdf.add_font('Noto', '', fonts/'NotoSans-Regular.ttf')
    pdf.add_font('Noto', 'B', fonts/'NotoSans-Bold.ttf')
    pdf.add_font('NotoCJK', '', fonts/'NotoSansSC-Regular.ttf')
    pdf.set_fallback_fonts(['NotoCJK'], exact_match=False)
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    color = str(branding.get('primary_color') or '#C47A1A')
    if not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
        color = '#C47A1A'
    pdf.set_fill_color(*(int(color[i:i+2], 16) for i in (1,3,5)))
    pdf.rect(0, 0, pdf.w, 7, 'F')
    pdf.set_y(14)
    logo = branding.get('logo_url')
    # Never resolve remote assets or arbitrary paths while exporting evidence.
    if isinstance(logo, str) and re.fullmatch(r'/static/branding/logo(?:-[0-9a-f]{32})?\.(?:png|jpg|webp)', logo):
        root = Path(__file__).resolve().parents[2] / 'static' / 'branding'
        path = (root / logo.rsplit('/',1)[1]).resolve()
        if path.is_relative_to(root.resolve()) and path.is_file() and path.stat().st_size <= 5*1024*1024:
            from PIL import Image
            with Image.open(path) as image:
                if image.format in ('PNG','JPEG','WEBP') and 0 < image.width*image.height <= 4_000_000:
                    image.verify()
                    pdf.image(path, x=pdf.w-45, y=14, w=30, h=15, keep_aspect_ratio=True)

    pdf.set_font('Noto', 'B', 19)
    pdf.multi_cell(pdf.epw-35, 10, str(branding.get('app_name') or 'O.D.I.N.'), new_x='LMARGIN',new_y='NEXT')
    pdf.set_font('Noto', 'B', 16)
    pdf.multi_cell(0, 9, 'Certificate of conformance',new_x='LMARGIN',new_y='NEXT')
    pdf.set_font('Noto', '', 10)
    status = record['inspection']['inspection_result']
    wording = {'pass':'Operator inspection: PASS.', 'fail':'Operator inspection: FAIL. Conformance is not established.'}.get(status,
        'Not inspected. Conformance is not established.')
    pdf.multi_cell(0, 6, f"Job {record['job_id']} / attempt {record['attempt']}\n{wording}\n"
        'This record attests to the evidence recorded in ODIN. The device signature proves origin and integrity, '
        'not independent certification. Unknown values are not recorded. See provenance below.',new_x='LMARGIN',new_y='NEXT')
    pdf.multi_cell(0, 5, f"Device key fingerprint (SHA-256): {identity[2]}\n"
                   f"Snapshot SHA-256: {record.get('snapshot_sha256', 'Not recorded')}\n"
                   'Unsupported glyphs are shown as [?]; JSON is authoritative.', new_x='LMARGIN', new_y='NEXT')
    if record.get('captured_after_completion'):
        pdf.multi_cell(0, 5, 'captured_after_completion: legacy facts captured after printing.', new_x='LMARGIN', new_y='NEXT')
    # Curated lifecycle contains only action/time/ID, never raw audit details.
    for name, value in jsonable_encoder(record).items():
        if name == 'audit_trail':
            continue
        if pdf.will_page_break(20):
            pdf.add_page()
        pdf.set_font('Noto', 'B', 11)
        pdf.multi_cell(0, 7, name.replace('_',' ').title(),new_x='LMARGIN',new_y='NEXT')
        pdf.set_font('Noto', '', 9)
        content = json.dumps(value, ensure_ascii=False, indent=2) if isinstance(value,(dict,list)) else ('Not recorded' if value is None else str(value))
        pdf.multi_cell(0, 5, content, new_x='LMARGIN',new_y='NEXT', wrapmode='CHAR')
    return bytes(pdf.output())
