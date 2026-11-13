"""2.2: the saved accent default is the designed amber; untouched green defaults move to it once."""
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend'))
from core.app_maintenance import _apply_amber_default_branding
from core.models import SystemConfig
from core.schema.bootstrap import import_all_models
from modules.organizations.branding import AMBER_DEFAULTS, Branding, default_branding_dict

import_all_models()
GREEN = {column: old for column, (old, _new) in AMBER_DEFAULTS.items()}
AMBER = {column: new for column, (_old, new) in AMBER_DEFAULTS.items()}


@pytest.fixture
def engine():
    engine = create_engine('sqlite://')
    Branding.__table__.create(engine)
    SystemConfig.__table__.create(engine)
    return engine


def saved(engine):
    with engine.connect() as connection:
        row = connection.execute(text('SELECT primary_color, accent_color, sidebar_active_text FROM branding WHERE id=1')).mappings().first()
    return dict(row) if row else None


def insert(engine, **colours):
    with engine.begin() as connection:
        connection.execute(text('INSERT INTO branding (id, primary_color, accent_color, sidebar_active_text) VALUES (1, :primary_color, :accent_color, :sidebar_active_text)'), colours)


def test_a_new_install_and_every_unlicensed_install_default_to_amber():
    stock = default_branding_dict()
    assert {key: stock[key] for key in AMBER} == AMBER == {'primary_color': '#dc9a3e', 'accent_color': '#e3a04a', 'sidebar_active_text': '#dc9a3e'}
    fresh = Branding(id=1)
    assert Branding.__table__.c.primary_color.default.arg == '#dc9a3e' and fresh.id == 1


def test_an_untouched_green_row_moves_to_amber(engine):
    insert(engine, **GREEN)
    assert _apply_amber_default_branding(engine) == 3
    assert saved(engine) == AMBER


def test_only_the_untouched_values_move(engine):
    """The farm on 2026-11-12: amber primary set by hand, accent and sidebar text still the old default."""
    insert(engine, primary_color='#c47821', accent_color='#4ADE80', sidebar_active_text='#4ade80')
    assert _apply_amber_default_branding(engine) == 2
    assert saved(engine) == {'primary_color': '#c47821', 'accent_color': '#e3a04a', 'sidebar_active_text': '#dc9a3e'}


def test_chosen_colours_are_left_alone(engine):
    chosen = {'primary_color': '#2563eb', 'accent_color': '#60a5fa', 'sidebar_active_text': '#ffffff'}
    insert(engine, **chosen)
    assert _apply_amber_default_branding(engine) == 0
    assert saved(engine) == chosen


def test_it_runs_once_so_a_later_deliberate_green_is_kept(engine):
    insert(engine, **GREEN)
    _apply_amber_default_branding(engine)
    with engine.begin() as connection:
        connection.execute(text("UPDATE branding SET accent_color='#4ade80' WHERE id=1"))
    assert _apply_amber_default_branding(engine) == 0
    assert saved(engine)['accent_color'] == '#4ade80'


def test_no_branding_row_yet_still_marks_done(engine):
    assert saved(engine) is None
    assert _apply_amber_default_branding(engine) == 0
    with engine.connect() as connection:
        assert connection.execute(text("SELECT 1 FROM system_config WHERE key='branding_amber_default_2_2'")).first()


def _ratio(a, b):
    def lum(colour):
        channels = [int(colour.lstrip('#')[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        r, g, b_ = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * r + 0.7152 * g + 0.0722 * b_
    light, dark = sorted((lum(a), lum(b)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def test_the_amber_defaults_are_readable_as_text_on_every_default_surface():
    stock = default_branding_dict()
    for surface in ('content_bg', 'card_bg', 'input_bg', 'sidebar_bg'):
        assert _ratio(stock['primary_color'], stock[surface]) >= 4.5, surface
    assert _ratio(stock['sidebar_active_text'], stock['sidebar_active_bg']) >= 4.5
    assert _ratio('#0B0D11', stock['primary_color']) >= 4.5      # dark button text on an amber button
    assert _ratio('#0B0D11', stock['accent_color']) >= 4.5       # and on its hover shade
