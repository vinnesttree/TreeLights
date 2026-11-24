"""Progress of a long job (decoding a walk, solving a map), for the web page.

Jobs run in worker processes, so progress goes through a small JSON file that the
server reads when the page asks for the job's status: the overall fraction done, the
current step and a detail line. Writes are throttled and atomic; a job without a file
reports nothing.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable

# A reporter for one part of a job: (fraction of that part done 0..1, detail).
Report = Callable[..., None]


def noop(frac: float = 0.0, detail: str = "") -> None:
    pass


def sub(report: Report | None, start: float, end: float, detail: str | None = None) -> Report:
    """A reporter for start..end of another reporter's range; a fixed detail if given."""
    if report is None:
        return noop

    def r(frac: float = 0.0, d: str = "") -> None:
        report(start + (end - start) * min(max(frac, 0.0), 1.0), detail if detail is not None and not d else d)

    return r


class Progress:
    MIN_INTERVAL_S = 0.5

    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self._last = 0.0
        self._last_kind = ""

    def part(self, start: float, end: float, step: str) -> Report:
        """A reporter for one step of the job, covering start..end of the whole."""

        def report(frac: float = 0.0, detail: str = "") -> None:
            # A new stage ("Building LED hypotheses") is always shown; counters ("150 of 1248
            # frames") are throttled. Stages are told apart by their text without digits.
            kind = step + "|" + "".join(c for c in detail if not c.isdigit())
            self.write(start + (end - start) * min(max(frac, 0.0), 1.0), step, detail, force=kind != self._last_kind)
            self._last_kind = kind

        self.write(start, step, "", force=True)
        return report

    def write(self, fraction: float, step: str, detail: str = "", force: bool = False) -> None:
        if self.path is None:
            return
        now = time.time()
        if not force and now - self._last < self.MIN_INTERVAL_S:
            return
        self._last = now
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(json.dumps({"fraction": round(float(fraction), 4), "step": step, "detail": detail, "t": now}))
            os.replace(tmp, self.path)
        except OSError:
            pass


def read(path: str | Path | None) -> dict | None:
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
