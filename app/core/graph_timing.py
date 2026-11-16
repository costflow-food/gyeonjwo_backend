"""Collect elapsed times for the current graph execution."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from time import monotonic

graph_timings = ContextVar('graph_timings', default=None)


@contextmanager
def measure_graph_stage(stage):
    timings = graph_timings.get()
    if timings is None:
        yield
        return
    started = monotonic()
    try:
        yield
    finally:
        timings[stage] += (monotonic() - started) * 1000


def timed_document(fn):
    @wraps(fn)
    def measured(*args, **kwargs):
        with measure_graph_stage('document'):
            return fn(*args, **kwargs)
    return measured
