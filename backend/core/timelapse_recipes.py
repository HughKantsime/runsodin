"""Typed durable recipe values; no filesystem or subprocess authority."""
from decimal import Decimal
from dataclasses import dataclass, asdict
import math

MAX_SECONDS = Decimal('9223372036854.775807')
MAX_CANONICAL_LENGTH = 340


def canonical_seconds(value):
    """Preserve the API number's supplied precision in ffmpeg's plain grammar."""
    if type(value) not in (int, float):
        raise ValueError('Timestamp must be a finite number')
    # Bound integers before formatting: untrusted huge integers must never
    # reach Python's integer-to-string conversion or Decimal allocation.
    if value < 0 or (type(value) is int and value > 9223372036854):
        raise ValueError('Timestamp outside supported range')
    if type(value) is float and not math.isfinite(value):
        raise ValueError('Timestamp must be a finite number')
    exact = Decimal(str(value))
    if exact > MAX_SECONDS:
        raise ValueError('Timestamp outside supported range')
    if exact == 0:
        return '0'
    plain = format(exact, 'f')
    if '.' in plain:
        plain = plain.rstrip('0').rstrip('.')
    if len(plain) > MAX_CANONICAL_LENGTH:
        raise ValueError('Timestamp outside supported range')
    return plain


def stored_seconds(value):
    """Validate saved text without normalizing or repairing its authority."""
    import re
    if type(value) is not str or not 1 <= len(value) <= MAX_CANONICAL_LENGTH:
        raise ValueError('Saved timestamp is not canonical')
    if re.fullmatch(r'(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?', value) is None:
        raise ValueError('Saved timestamp is not canonical')
    exact = Decimal(value)
    if exact > MAX_SECONDS:
        raise ValueError('Saved timestamp outside supported range')
    return exact


RECIPE_COLUMNS = ('recipe_version', 'encode_fps', 'speed_numerator',
                  'speed_denominator', 'trim_start', 'trim_end')
SPEED_PAIRS = {(1, 2), (1, 1), (3, 2), (2, 1), (4, 1), (8, 1)}
SPEED_VALUES = {0.5: (1, 2), 1: (1, 1), 1.5: (3, 2),
                2: (2, 1), 4: (4, 1), 8: (8, 1)}


@dataclass(frozen=True)
class Recipe:
    kind: str
    recipe_version: int
    encode_fps: int | None = None
    speed_numerator: int | None = None
    speed_denominator: int | None = None
    trim_start: str | None = None
    trim_end: str | None = None

    def __post_init__(self):
        if type(self.recipe_version) is not int or self.recipe_version != 1:
            raise ValueError('Unsupported recipe version')
        if type(self.kind) is not str or self.kind not in ('frame', 'encode', 'trim', 'speed'):
            raise ValueError('Unsupported recipe kind')
        if self.kind == 'encode':
            if type(self.encode_fps) is not int or self.encode_fps != 30:
                raise ValueError('Unsupported encoding FPS')
        elif self.encode_fps is not None:
            raise ValueError('Unexpected encoding FPS')
        if self.kind == 'speed':
            if (type(self.speed_numerator) is not int or
                    type(self.speed_denominator) is not int or
                    (self.speed_numerator, self.speed_denominator) not in SPEED_PAIRS):
                raise ValueError('Unsupported speed ratio')
        elif self.speed_numerator is not None or self.speed_denominator is not None:
            raise ValueError('Unexpected speed ratio')
        if self.kind == 'trim':
            if stored_seconds(self.trim_start) >= stored_seconds(self.trim_end):
                raise ValueError('Invalid saved trim range')
        elif self.trim_start is not None or self.trim_end is not None:
            raise ValueError('Unexpected trim range')

    def columns(self):
        return {key: value for key, value in asdict(self).items() if key != 'kind'}


def make_recipe(kind, *, start_seconds=None, end_seconds=None, multiplier=None):
    values = {'kind': kind, 'recipe_version': 1}
    if kind == 'trim':
        values.update(trim_start=canonical_seconds(start_seconds),
                      trim_end=canonical_seconds(end_seconds))
    elif start_seconds is not None or end_seconds is not None:
        raise ValueError('Unexpected trim parameters')
    if kind == 'speed':
        if type(multiplier) not in (int, float) or multiplier not in SPEED_VALUES:
            raise ValueError('Unsupported speed multiplier')
        values['speed_numerator'], values['speed_denominator'] = SPEED_VALUES[multiplier]
    elif multiplier is not None:
        raise ValueError('Unexpected speed multiplier')
    if kind == 'encode':
        values['encode_fps'] = 30
    return Recipe(**values)


def recipe_from_row(row):
    try:
        return Recipe(kind=row['kind'], **{key: row[key] for key in RECIPE_COLUMNS})
    except KeyError:
        raise ValueError('Saved recipe fields missing') from None
