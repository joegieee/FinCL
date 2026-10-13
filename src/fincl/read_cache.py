"""Compute each composed reader once per request.

The forecast, "Will I have enough" and the controls page are built from the
same baseline, readiness, obligations and position readers, and each of those
calls the others. Within one request they all read one database snapshot, so
every repeat call returns the same answer; on a real ledger the repeats cost
most of the request (16 readiness passes for one forecast).

``request_cache()`` opens a scope for one request on one connection. Inside it
a ``@per_request`` reader runs once per (connection, arguments) and hands each
caller its own deep copy, so a caller mutating its result cannot change what
the next caller sees. Outside a scope nothing is cached. The scope pins the
connection to one read transaction, so a cached answer can never describe a
different snapshot than a fresh one would.
"""
from __future__ import annotations

import copy
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any

_SCOPE: ContextVar[dict[tuple[Any, ...], Any] | None] = ContextVar("fincl_request_cache", default=None)

@contextmanager
def request_cache(connection: sqlite3.Connection) -> Iterator[None]:
    """One request's cache. Nested scopes share the outermost one."""
    if _SCOPE.get() is not None:
        yield
        return
    if not connection.in_transaction:
        connection.execute("BEGIN")
    token = _SCOPE.set({})
    try:
        yield
    finally:
        _SCOPE.reset(token)


def per_request[F: Callable[..., Any]](fn: F) -> F:
    """Memoize a ``fn(connection, **keywords)`` reader inside a request scope."""

    @wraps(fn)
    def wrapper(connection: sqlite3.Connection, *args: Any, **kwargs: Any) -> Any:
        scope = _SCOPE.get()
        if scope is None or not connection.in_transaction:
            return fn(connection, *args, **kwargs)
        key = (fn.__module__, fn.__qualname__, id(connection), args, tuple(sorted(kwargs.items())))
        try:
            hash(key)
        except TypeError:  # an unhashable argument: not worth guessing an identity for
            return fn(connection, *args, **kwargs)
        if key not in scope:
            scope[key] = fn(connection, *args, **kwargs)
        return copy.deepcopy(scope[key])

    return wrapper  # type: ignore[return-value]
