"""Short synchronous critical sections; never hold these locks across await."""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from threading import RLock
from typing import Concatenate, ParamSpec, Protocol, TypeVar

P = ParamSpec("P")
R = TypeVar("R")


class LockOwner(Protocol):
    runtime_lock: RLock


Owner = TypeVar("Owner", bound=LockOwner)
_registry_lock = RLock()
_project_locks: dict[str, RLock] = {}


def project_runtime_lock(project_id: str) -> RLock:
    with _registry_lock:
        return _project_locks.setdefault(project_id, RLock())


def project_locked(
    method: Callable[Concatenate[str, P], R],
) -> Callable[Concatenate[str, P], R]:
    @wraps(method)
    def wrapped(project_id: str, /, *args: P.args, **kwargs: P.kwargs) -> R:
        with project_runtime_lock(project_id):
            return method(project_id, *args, **kwargs)

    return wrapped


def session_locked(
    method: Callable[Concatenate[Owner, P], R],
) -> Callable[Concatenate[Owner, P], R]:
    @wraps(method)
    def wrapped(owner: Owner, /, *args: P.args, **kwargs: P.kwargs) -> R:
        with owner.runtime_lock:
            return method(owner, *args, **kwargs)

    return wrapped
