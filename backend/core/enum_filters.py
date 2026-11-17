"""Validate string filters before comparing them with native database enums."""
from enum import Enum

from fastapi import HTTPException


def enum_filter(value: str, enum: type[Enum], parameter: str) -> str:
    if value not in {member.value for member in enum}:
        allowed = ", ".join(member.value for member in enum)
        raise HTTPException(422, f"{parameter} must be one of: {allowed}.")
    return value
