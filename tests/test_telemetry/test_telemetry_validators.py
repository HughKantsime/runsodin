"""Contract tests for telemetry polymorphic validators.

These cover T1.2 of track telemetry-rewrite-bambu-first_20260417 — the
coercion primitives that make Bambu's int/str polymorphism explicit and
fail-loud instead of silently degrading.
"""
from __future__ import annotations

import pytest

from backend.modules.printers.telemetry.validators import (
    coerce_float_or_raise,
    coerce_int_or_raise,
)


class TestCoerceInt:
    def test_accepts_int(self):
        assert coerce_int_or_raise(2) == 2
        assert coerce_int_or_raise(-1) == -1
        assert coerce_int_or_raise(255) == 255

    def test_accepts_str_of_int(self):
        # Bambu captures bambu-p1s/bambu-h2d show stg_cur as both int and str
        assert coerce_int_or_raise("2") == 2
        assert coerce_int_or_raise("255") == 255
        assert coerce_int_or_raise("-1") == -1

    def test_accepts_str_with_whitespace(self):
        assert coerce_int_or_raise("  42  ") == 42

    def test_rejects_bool(self):
        # bool is subclass of int; refuse to mask type errors
        with pytest.raises(ValueError, match="refusing to coerce bool"):
            coerce_int_or_raise(True)
        with pytest.raises(ValueError, match="refusing to coerce bool"):
            coerce_int_or_raise(False)

    def test_rejects_float(self):
        with pytest.raises(ValueError, match="cannot coerce float"):
            coerce_int_or_raise(2.5)

    def test_rejects_non_numeric_str(self):
        with pytest.raises(ValueError):
            coerce_int_or_raise("abc")

    def test_rejects_empty_str(self):
        with pytest.raises(ValueError, match="empty string"):
            coerce_int_or_raise("")
        with pytest.raises(ValueError, match="empty string"):
            coerce_int_or_raise("   ")

    def test_rejects_none(self):
        with pytest.raises(ValueError, match="cannot coerce NoneType"):
            coerce_int_or_raise(None)

    def test_rejects_list(self):
        with pytest.raises(ValueError, match="cannot coerce list"):
            coerce_int_or_raise([1])


class TestCoerceFloat:
    def test_accepts_int(self):
        # Bambu bed_temper captured as both int (35) and float (35.4)
        assert coerce_float_or_raise(35) == 35.0
        assert isinstance(coerce_float_or_raise(35), float)

    def test_accepts_float(self):
        assert coerce_float_or_raise(35.4) == 35.4

    def test_accepts_str_of_number(self):
        assert coerce_float_or_raise("35.4") == 35.4
        assert coerce_float_or_raise("35") == 35.0

    def test_rejects_bool(self):
        with pytest.raises(ValueError, match="refusing to coerce bool"):
            coerce_float_or_raise(True)

    def test_rejects_non_numeric_str(self):
        with pytest.raises(ValueError):
            coerce_float_or_raise("hot")

    def test_rejects_empty_str(self):
        with pytest.raises(ValueError, match="empty string"):
            coerce_float_or_raise("")

    def test_rejects_none(self):
        with pytest.raises(ValueError, match="cannot coerce NoneType"):
            coerce_float_or_raise(None)


class TestDeterminism:
    """Same input, same output, always. No hidden state."""

    def test_int_is_deterministic(self):
        results = [coerce_int_or_raise("42") for _ in range(10)]
        assert all(r == 42 for r in results)

    def test_float_is_deterministic(self):
        results = [coerce_float_or_raise("35.4") for _ in range(10)]
        assert all(r == 35.4 for r in results)
