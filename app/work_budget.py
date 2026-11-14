"""Request-local monotonic deadline shared by rendering and downstream work."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import time

_deadline = ContextVar('kb_work_deadline', default=None)


class Exhausted(RuntimeError):
    pass


def remaining():
    deadline = _deadline.get()
    return float('inf') if deadline is None else max(0, deadline-time.monotonic())


def timeout(default, reserve=0):
    left = remaining()-reserve
    if left <= .1:
        raise Exhausted('本次剩余时间不足，材料已保留待下次重试')
    return min(default, left)


@contextmanager
def limit(seconds):
    deadline = min(_deadline.get() or float('inf'), time.monotonic()+seconds)
    token = _deadline.set(deadline)
    try:
        yield
    finally:
        _deadline.reset(token)


def limited(seconds):
    def decorate(fn):
        @wraps(fn)
        def call(*args, **kwargs):
            with limit(seconds):
                return fn(*args, **kwargs)
        return call
    return decorate
