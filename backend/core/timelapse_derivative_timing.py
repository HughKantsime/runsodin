"""Recipe-v1 CFR30 transform, using exact integer/rational frame identities."""
from fractions import Fraction
from core.timelapse_recipes import Recipe, stored_seconds

MAX_FRAMES = 32768
FPS = 30

def ceil_fraction(value):
    return -(-value.numerator // value.denominator)

def frame_plan(recipe, count):
    if type(recipe) is not Recipe or type(count) is not int or not 1 <= count <= MAX_FRAMES:
        raise ValueError('Unsupported derivative source')
    if recipe.kind == 'trim':
        start = Fraction(stored_seconds(recipe.trim_start))
        end = Fraction(stored_seconds(recipe.trim_end))
        if end > Fraction(count, FPS):
            raise ValueError('Trim exceeds source duration')
        first, stop = ceil_fraction(start * FPS), ceil_fraction(end * FPS)
        if not 0 <= first < stop <= count:
            raise ValueError('Trim contains no source frame')
        indices = range(first, stop)
    elif recipe.kind == 'speed':
        p, q = recipe.speed_numerator, recipe.speed_denominator
        size = ceil_fraction(Fraction(count * q, p))
        if size > MAX_FRAMES:
            raise ValueError('Derivative exceeds reusable frame limit')
        indices = tuple(j * p // q for j in range(size))
    else:
        raise ValueError('Unsupported derivative recipe')
    if not 1 <= len(indices) <= MAX_FRAMES:
        raise ValueError('Derivative frame limit')
    return indices

def validate_timestamps(timestamps, time_base):
    if type(timestamps) is not list or not 1 <= len(timestamps) <= MAX_FRAMES:
        raise ValueError('Unsupported derivative frame count')
    base = Fraction(time_base)
    if base <= 0 or any(type(stamp) is not int or Fraction(stamp) * base != Fraction(i, FPS)
                        for i, stamp in enumerate(timestamps)):
        raise ValueError('Only zero-origin CFR30 media is supported')
    return len(timestamps)
