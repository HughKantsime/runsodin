"""Request-local wall-time spans, including work copied into AnyIO worker threads.

Intervals are partitioned, not summed: the deepest active span owns each instant.
Overlapping sibling tasks use the most recently entered span. Sequential mode
records active coroutine segments and process CPU intervals; concurrent mode
retains elapsed spans including suspension. SQL is also an inclusive diagnostic;
only the partitioned database row is additive.
"""

from __future__ import annotations

import inspect
import sys
import time
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from types import coroutine


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
    cpu_started: float = 0
    cpu_ended: float = 0
    cpu_intervals: list = field(default_factory=list)
    sql_trace: list | None = None

    def partition(self, cpu=False):
        """Sweep interval boundaries without double-counting concurrent spans."""
        started, ended, intervals = ((self.cpu_started, self.cpu_ended, self.cpu_intervals) if cpu
                                     else (self.started, self.ended, self.intervals))
        events = [(started, 1, -1), (ended, -1, -1)]
        for index, (start, end, _, _) in enumerate(intervals):
            start, end = max(start, started), min(end, ended)
            if end > start:
                events.extend(((start, 1, index), (end, -1, index)))
        active = set()
        previous = started
        layers = defaultdict(float)
        for instant, direction, index in sorted(events):
            owner = max(active, key=lambda i: (intervals[i][3], intervals[i][0], i), default=None)
            label = intervals[owner][2] if owner is not None else "framework_wait"
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
    cpu_started = time.process_time()
    try:
        yield
    finally:
        sample.intervals.append((started, time.perf_counter(), label, depth))
        sample.cpu_intervals.append((cpu_started, time.process_time(), label, depth))
        _depth.reset(token)
        _layer.reset(layer_token)


@coroutine
def active_await(awaitable, label):
    """Time only execution between yields, forwarding results and cancellation.

    A span across an await bills event-loop suspension to its caller. Stepping
    the await iterator keeps nested calls inside the span but pauses its clock
    whenever control returns to the event loop (including worker-thread waits).
    """
    iterator = awaitable.__await__()
    value, error = None, None
    while True:
        try:
            with span(label):
                yielded = iterator.throw(error) if error is not None else iterator.send(value)
        except StopIteration as done:
            return done.value
        try:
            value = yield yielded
            error = None
        except BaseException as exc:
            value, error = None, exc


def timed(function, label, active_only=False):
    if inspect.iscoroutinefunction(function):
        @wraps(function)
        async def asynchronous(*args, **kwargs):
            if active_only:
                return await active_await(function(*args, **kwargs), label)
            with span(label):
                return await function(*args, **kwargs)
        return asynchronous

    @wraps(function)
    def synchronous(*args, **kwargs):
        with span(label):
            return function(*args, **kwargs)
    return synchronous


class Profile:
    def __init__(self, sequential=False):
        self.sequential = sequential
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
        replacement = timed(function, label, self.sequential)
        self.patch(target, name, replacement)
        return function, replacement

    @contextmanager
    def request(self, category, statements=False):
        sample = Sample(category, time.perf_counter(), cpu_started=time.process_time(),
                        sql_trace=[] if statements else None)
        token = _request.set(sample)
        depth = _depth.set(0)
        layer = _layer.set("framework_wait")
        try:
            yield sample
        finally:
            sample.ended = time.perf_counter()
            sample.cpu_ended = time.process_time()
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
                if sample.sql_trace is not None:
                    sample.sql_trace.append({"statement": statement, "source": statement_source()})
                context._request_profile_span = (sample, time.perf_counter(), time.process_time(),
                                                 _depth.get() + 1, _layer.get())

        def finish(context):
            saved = getattr(context, "_request_profile_span", None)
            if saved is not None:
                del context._request_profile_span
                sample, started, cpu_started, depth, label = saved
                ended = time.perf_counter()
                sample.sql_seconds += ended - started
                sample.sql_by_layer[label] += (ended - started) * 1000
                sample.intervals.append((started, ended, "database", depth))
                sample.cpu_intervals.append((cpu_started, time.process_time(), "database", depth))

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
            outer_label = "framework_wait"
        else:
            function = getattr(node, "dispatch_func", type(node).__call__)
            label = "middleware." + (function.__name__ if hasattr(node, "dispatch_func") else type(node).__name__)
            self.sources[label] = source_name(function)
            if self.sequential and hasattr(node, "dispatch_func"):
                # Attribute only dispatch's own code. BaseHTTPMiddleware's
                # task groups, streams and call_next belong to the framework.
                async def dispatch(request, call_next):
                    return await timed(function, label, True)(request, timed(call_next, "framework_wait", True))

                self.patch(node, "dispatch_func", dispatch)
                outer_label = "framework_wait"
            else:
                outer_label = label
            child = node.app
            wrapped_child = self.middleware(child)

            async def downstream(scope, receive, send):
                # This send is the current node's callback. Its work happens
                # inside its child, so give it its own nested interval.
                return await wrapped_child(scope, receive, timed(send, outer_label, self.sequential))

            self.patch(node, "app", downstream)

        async def measured(scope, receive, send):
            if self.sequential:
                return await active_await(node(scope, receive, send), outer_label)
            with span(outer_label):
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

    def distributions(self, samples):
        groups = defaultdict(list)
        for sample in samples:
            groups[sample.category].append(sample)
            groups["all"].append(sample)
        result = {}
        for category, rows in sorted(groups.items()):
            wall = [sample.partition() for sample in rows]
            cpu = [sample.partition(cpu=True) for sample in rows]
            labels = sorted(self.sources.keys() | set().union(*wall, *cpu))
            result[category] = {
                "requests": len(rows),
                "wall_ms": distribution([(s.ended - s.started) * 1000 for s in rows]),
                "cpu_ms": distribution([(s.cpu_ended - s.cpu_started) * 1000 for s in rows]),
                "statements": distribution([s.statements for s in rows]),
                "sql_ms": distribution([s.sql_seconds * 1000 for s in rows]),
                "layers_wall_ms": {label: distribution([row.get(label, 0) for row in wall]) for label in labels},
                "layers_cpu_ms": {label: distribution([row.get(label, 0) for row in cpu]) for label in labels},
            }
        return result


def distribution(values):
    """Linearly interpolated request percentiles, not percentiles of run means."""
    ordered = sorted(values)

    def percentile(fraction):
        position = (len(ordered) - 1) * fraction
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)

    return {"median": percentile(.5), "p10": percentile(.1), "p90": percentile(.9)}


def statement_source():
    """Nearest product frame issuing SQL; never inspect locals or parameters."""
    frame = sys._getframe(1)
    fallback = None
    try:
        while frame is not None:
            filename = frame.f_code.co_filename.replace("\\", "/")
            if "/backend/" in filename:
                return "backend/" + filename.split("/backend/", 1)[1] + ":" + frame.f_code.co_name
            if fallback is None and filename != __file__ and "/sqlalchemy/" not in filename:
                fallback = filename + ":" + frame.f_code.co_name
            frame = frame.f_back
        return fallback or "unknown"
    finally:
        del frame
