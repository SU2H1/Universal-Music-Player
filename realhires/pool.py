"""Process pool without Python's Windows limit of 61 workers.

On Windows, ``concurrent.futures.ProcessPoolExecutor`` refuses more than 61
workers, because it waits on all worker handles with ``WaitForMultipleObjects``,
which accepts at most 64 handles (3 are reserved).  ``MultiProcessPool`` spreads
the requested workers over several executors, each within the limit, so a
64-core Windows workstation can use all of its cores.
"""

from __future__ import annotations

import itertools
import os
import sys
from concurrent.futures import Future, ProcessPoolExecutor
from typing import List, Optional

WINDOWS_POOL_LIMIT = 61
MAX_JOBS = 512


def cpu_count() -> int:
    try:
        return len(os.sched_getaffinity(0))  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def effective_jobs(jobs: Optional[int]) -> int:
    """0/None = automatic (one per CPU core); otherwise clamp to 1..MAX_JOBS."""
    if not jobs or jobs <= 0:
        return cpu_count()
    return max(1, min(int(jobs), MAX_JOBS))


def split_workers(workers: int, limit: Optional[int] = None) -> List[int]:
    """Split `workers` into executor sizes that each respect `limit`."""
    if limit is None:
        limit = WINDOWS_POOL_LIMIT if sys.platform.startswith("win") else workers
    workers = max(1, workers)
    limit = max(1, limit)
    n = -(-workers // limit)  # ceil
    base, extra = divmod(workers, n)
    return [base + (1 if i < extra else 0) for i in range(n)]


class MultiProcessPool:
    """Minimal executor facade (submit/shutdown/context manager) over one or
    more ProcessPoolExecutors.  Tasks are dealt out round-robin."""

    def __init__(self, workers: int, mp_context=None, limit: Optional[int] = None, initializer=None,
                 initargs: tuple = ()):
        self.sizes = split_workers(workers, limit)
        self._pools = [ProcessPoolExecutor(max_workers=n, mp_context=mp_context, initializer=initializer,
                                           initargs=initargs) for n in self.sizes]
        self._next = itertools.cycle(self._pools)

    @property
    def workers(self) -> int:
        return sum(self.sizes)

    def submit(self, fn, *args, **kwargs) -> Future:
        return next(self._next).submit(fn, *args, **kwargs)

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        for p in self._pools:
            p.shutdown(wait=wait, cancel_futures=cancel_futures)

    def __enter__(self) -> "MultiProcessPool":
        return self

    def __exit__(self, *exc) -> None:
        self.shutdown(wait=True)
