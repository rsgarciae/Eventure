"""Fuzz target for the free-text date parser.

    python fuzz/fuzz_dates.py -max_total_time=60

`dates.py` is the one place that turns untrusted text — an Instagram caption, an
RSS title — into a `datetime` that later lands in a calendar and in a Telegram
message. The properties worth holding are cheap to state:

    * it must never raise, whatever bytes arrive;
    * every date it returns must be a real date in a sane year;
    * results must be ordered, and `find()` must agree with `find_all()`.

Run from the repository root so `eventure` is importable, or let the sys.path
line below handle it.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import atheris  # noqa: E402  (must come after sys.path fix-up)

from eventure import dates  # noqa: E402

TZ = timezone.utc
REFERENCE = datetime(2026, 1, 1, 12, tzinfo=TZ)


def test_one_input(data: bytes) -> None:
    text = data.decode("utf-8", errors="replace")

    found = dates.find_all(text, TZ, REFERENCE, "DMY", ("es", "en"))

    previous = None
    for item in found:
        if not 2000 <= item.start.year <= 2100:
            raise AssertionError(f"implausible year: {item.start.isoformat()}")
        if item.start.microsecond:
            raise AssertionError("sub-second precision in a date")
        if previous is not None and item.start < previous:
            raise AssertionError("results are not ordered")
        previous = item.start

    single = dates.find(text, TZ, REFERENCE, "DMY", ("es", "en"))
    if (single is None) != (not found):
        raise AssertionError("find() disagrees with find_all()")
    if single is not None and single.start != found[0].start:
        raise AssertionError("find() is not the first of find_all()")


def main() -> None:
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
