"""Playwright control — launch headless Chromium, install the demo session
cookie, navigate to a target route, wait for `data-capture` selectors,
and hand back a Page handle for screenshot/video capture.

Wake 2 deliverable: full implementation. Wake 1 is signatures only.

Hard requirement: headless from Wake 2 forward. The directive's hard
stops include "If Playwright requires a GUI display for screenshot mode,
halt (use headless from start)" — so all browser launches default
headless and nothing is added to opt out without an explicit flag.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator


@dataclass
class Viewport:
    width: int
    height: int


@contextmanager
def open_browser(viewport: Viewport, *, headless: bool = True) -> Iterator[object]:
    """Launch Playwright Chromium, yield a Page-like handle, tear down on exit.

    Wake 2 deliverable. Until then this raises NotImplementedError so a
    caller cannot accidentally rely on it.
    """
    raise NotImplementedError("open_browser: Wake 2 deliverable")
    yield  # for type-checker happiness; never reached
