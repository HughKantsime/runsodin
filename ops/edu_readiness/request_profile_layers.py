"""Request-local wall-time spans, including work copied into AnyIO worker threads.

Intervals are partitioned, not summed: the deepest active span owns each instant.
Overlapping sibling tasks use the most recently entered span. These are elapsed
times (including suspension), not CPU measurements. SQL is also reported as an
inclusive diagnostic; only the partitioned database row is additive.
"""

from __future__ import annotations

import inspect
import time
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps


_request = ContextVar("request_profile", default=None)
_depth = ContextVar("request_profile_depth", default=0)
_layer = ContextVar("request_profile_layer", default="framework_wait")


def source_name(function):
    filename = inspect.getsourcefile(function) or function.__module__
    if "/backend/" in filename:
        filename = "backend/" + filename.split("/backend/", 1)[1]
    return f"{filename}:{function.__qualname__}"


@dataclass
class Sample:
    category: str
    started: float
    ended: float = 0
    intervals: list = field(default_factory=list)
    statements: int = 0
    sql_seconds: float = 0
    sql_by_layer: dict = field(default_factory=lambda: defaultdict(float))

    def partition(self):
        """Sweep interval boundaries without double-counting concurrent spans."""
        events = [(self.started, 1, -1), (self.ended, -1, -1)]
        for index, (start, end, _, _) in enumerate(self.intervals):
            start, end = max(start, self.started), min(end, self.ended)
            if end > start:
                events.extend(((start, 1, index), (end, -1, index)))
        active = set()
        previous = self.started
        layers = defaultdict(float)
        for instant, direction, index in sorted(events):
            owner = max(active, key=lambda i: (self.intervals[i][3], self.intervals[i][0], i), default=None)
            label = self.intervals[owner][2] if owner is not None else "framework_wait"
            if instant > previous:
                layers[label] += (instant - previous) * 1000
            if index >= 0:
                if direction == 1:
                    active.add(index)
                else:
                    active.discard(index)
            previous = instant
        return dict(layers)


@contextmanager
def span(label):
    sample = _request.get()
    if sample is None:
        yield
        return
    depth = _depth.get() + 1
    token = _depth.set(depth)
    layer_token = _layer.set(label)
    started = time.perf_counter()
    try:
        yield
    finally:
        sample.intervals.append((started, time.perf_counter(), label, depth))
        _depth.reset(token)
        _layer.reset(layer_token)


def timed(function, label):
    if inspect.iscoroutinefunction(function):
        @wraps(function)
        async def asynchronous(*args, **kwargs):
            with span(label):
                return await function(*args, **kwargs)
        return asynchronous

    @wraps(function)
    def synchronous(*args, **kwargs):
        with span(label):
            return function(*args, **kwargs)
    return synchronous


class Profile:
    def __init__(self):
        self.samples = []
        self.sources = {"framework_wait": "HTTPX / Starlette / FastAPI plumbing and scheduling",
                        "database": "SQLAlchemy before_cursor_execute / after_cursor_execute"}
        self._restores = []

    def patch(self, target, name, value):
        previous = getattr(target, name)
        self._restores.append((target, name, previous))
        setattr(target, name, value)

    def wrap(self, target, name, label):
        function = getattr(target, name)
        source = source_name(function)
        existing = self.sources.get(label, "").split("; ")
        if source not in existing and label != "route_handler":
            self.sources[label] = "; ".join(value for value in (*existing, source) if value)
        replacement = timed(function, label)
        self.patch(target, name, replacement)
        return function, replacement

    @contextmanager
    def request(self, category):
        sample = Sample(category, time.perf_counter())
        token = _request.set(sample)
        depth = _depth.set(0)
        layer = _layer.set("framework_wait")
        try:
            yield sample
        finally:
            sample.ended = time.perf_counter()
            _request.reset(token)
            _depth.reset(depth)
            _layer.reset(layer)
            self.samples.append(sample)

    @contextmanager
    def database(self, engine):
        from sqlalchemy import event

        def before(conn, cursor, statement, parameters, context, executemany):
            sample = _request.get()
            if sample is not None:
                sample.statements += 1
                context._request_profile_span = (sample, time.perf_counter(), _depth.get() + 1, _layer.get())

        def finish(context):
            saved = getattr(context, "_request_profile_span", None)
            if saved is not None:
                del context._request_profile_span
                sample, started, depth, label = saved
                ended = time.perf_counter()
                sample.sql_seconds += ended - started
                sample.sql_by_layer[label] += (ended - started) * 1000
                sample.intervals.append((started, ended, "database", depth))

        def after(conn, cursor, statement, parameters, context, executemany):
            finish(context)

        def error(exception_context):
            finish(exception_context.execution_context)

        listeners = (("before_cursor_execute", before), ("after_cursor_execute", after), ("handle_error", error))
        for name, callback in listeners:
            event.listen(engine, name, callback)
        try:
            yield
        finally:
            for name, callback in listeners:
                event.remove(engine, name, callback)

    def middleware(self, node):
        """Instrument the actual ASGI stack, including response-send callbacks."""
        if not hasattr(node, "app"):
            label = "framework_wait"
        else:
            function = getattr(node, "dispatch_func", type(node).__call__)
            label = "middleware." + (function.__name__ if hasattr(node, "dispatch_func") else type(node).__name__)
            self.sources[label] = source_name(function)
            child = node.app
            wrapped_child = self.middleware(child)

            async def downstream(scope, receive, send):
                # This send is the current node's callback. Its work happens
                # inside its child, so give it its own nested interval.
                return await wrapped_child(scope, receive, timed(send, label))

            self.patch(node, "app", downstream)

        async def measured(scope, receive, send):
            with span(label):
                return await node(scope, receive, send)
        return measured

    def restore(self):
        for target, name, previous in reversed(self._restores):
            setattr(target, name, previous)
        self._restores.clear()

    def table(self):
        groups = defaultdict(list)
        for sample in self.samples:
            groups[sample.category].append(sample)
            groups["all"].append(sample)
        result = {}
        for category, samples in sorted(groups.items()):
            layers = defaultdict(float)
            for sample in samples:
                for label, value in sample.partition().items():
                    layers[label] += value
            count = len(samples)
            sql_owners = set().union(*(s.sql_by_layer for s in samples))
            result[category] = {
                "requests": count,
                "total_ms": sum((s.ended - s.started) * 1000 for s in samples) / count,
                "layers_ms": {label: layers[label] / count for label in sorted(self.sources.keys() | layers.keys())},
                "statements": sum(s.statements for s in samples) / count,
                "sql_ms_inclusive": sum(s.sql_seconds * 1000 for s in samples) / count,
                "sql_by_layer_ms": {label: sum(s.sql_by_layer.get(label, 0) for s in samples) / count
                                    for label in sorted(sql_owners)},
            }
        return result
