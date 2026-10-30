"""Pure local palette reads: no application startup, credentials or live database."""
import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend'))
from modules.organizations.branding import (
    Branding, LEGACY_TEXT_ON_SURFACES, branding_to_dict, default_branding_dict,
)
from core.schema.bootstrap import import_all_models
# Instantiating Branding configures every mapper; a batch that starts here must not leave Job's Model unresolved.
import_all_models()


# Prod's saved row on 2026-10-29 (colours only).
PROD_PALETTE = {
    "primary_color": "#c47821", "accent_color": "#4ade80", "sidebar_bg": "#1a1917",
    "sidebar_border": "#3b3934", "sidebar_text": "#8a8679", "sidebar_active_bg": "#3b3934",
    "sidebar_active_text": "#4ade80", "content_bg": "#1a1917", "card_bg": "#33312d",
    "card_border": "#3b3934", "text_primary": "#e5e4e1", "text_secondary": "#8a8679",
    "text_muted": "#58554a", "input_bg": "#3b3934", "input_border": "#47453d",
}


def test_legacy_defaults_resolve_without_mutating_or_writing():
    engine = create_engine('sqlite://')
    Branding.__table__.create(engine)
    with Session(engine) as db:
        row = Branding(id=1, app_name='School name', **PROD_PALETTE)
        db.add(row)
        db.commit()
        before = dict(db.execute(select(Branding.__table__)).mappings().one())
        statements = []
        event.listen(engine, 'before_cursor_execute', lambda conn, cursor, statement, *args: statements.append(statement))
        result = branding_to_dict(row)
        assert result['text_secondary'] == '#bcb7aa'
        assert result['text_muted'] == '#a8a294'
        assert result['app_name'] == 'School name'
        assert not db.dirty
        db.flush()
        assert dict(db.execute(select(Branding.__table__)).mappings().one()) == before
        assert all(sql.lstrip().upper().startswith('SELECT') for sql in statements)


@pytest.mark.parametrize('field', LEGACY_TEXT_ON_SURFACES)
def test_a_custom_text_or_surface_colour_preserves_the_palette(field):
    colors = {**PROD_PALETTE, field: '#123456'}
    result = branding_to_dict(Branding(**colors))
    assert {key: result[key] for key in colors} == colors


def test_custom_brand_colours_still_get_readable_text():
    # Prod (2026-10-29): amber primary, legacy text and surfaces -> upgraded text only.
    result = branding_to_dict(Branding(**{**PROD_PALETTE, 'primary_color': '#C47821'}))
    assert (result['text_secondary'], result['text_muted']) == ('#bcb7aa', '#a8a294')
    assert result['primary_color'] == '#C47821' and result['card_bg'] == '#33312d'


def test_new_defaults_match_frontend_tokens():
    defaults = default_branding_dict()
    tokens = json.loads((Path(__file__).resolve().parents[2] / 'design/tokens.json').read_text())
    for name in ('primary', 'secondary', 'muted'):
        assert defaults[f'text_{name}'] == tokens['text']['dark'][name]
    assert defaults['card_bg'] == tokens['surface']['dark']['card']


@pytest.mark.parametrize('mode', ['dark', 'light', 'highContrast'])
def test_all_default_theme_text_meets_aa(mode):
    root = Path(__file__).resolve().parents[2]
    tokens = json.loads((root / 'design/tokens.json').read_text())
    palette = {'text': tokens['text']['dark'], 'surface': tokens['surface']['dark']} if mode == 'dark' else tokens['modes'][mode]

    def luminance(hex_color):
        channels = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return sum(c * weight for c, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

    # Check emitted CSS too: changing tokens without regenerating must fail.
    css = (root / 'frontend/src/design-tokens.css').read_text()
    selector = {'dark': ':root', 'light': 'html.light', 'highContrast': '.high-contrast'}[mode]
    block = css.split(selector + ' {', 1)[1].split('}', 1)[0]
    for name in ('primary', 'secondary', 'muted'):
        color = palette['text'][name]
        assert f'--brand-text-{name}: {color};' in block
        for surface, css_name in [('card', 'card-bg'), ('bg', 'content-bg'), ('inputBg', 'input-bg'), ('surface', 'surface')]:
            background = palette['surface'][surface]
            assert f'--brand-{css_name}: {background};' in block
            dark, light = sorted((luminance(color), luminance(background)))
            assert (light + 0.05) / (dark + 0.05) >= 4.5, (mode, name, surface)
