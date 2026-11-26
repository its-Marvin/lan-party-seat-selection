"""Event status depends on the clock: tests run at a fixed local time, so their
example dates (December 2026) stay in the future no matter when they run."""

from datetime import datetime

import pytest

from lanparty import events

FROZEN_NOW = datetime(2026, 6, 1, 12, 0)


@pytest.fixture(autouse=True)
def frozen_event_clock(monkeypatch):
    clock = {"now": FROZEN_NOW}
    monkeypatch.setattr(events, "local_now", lambda: clock["now"])
    return clock
