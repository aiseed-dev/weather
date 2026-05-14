# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Selects the most recent forecast run that is guaranteed to be published.

ECMWF publishes each run roughly 6 hours after its nominal time. We pick the
latest run satisfying (now - run_time) >= PUBLICATION_DELAY so downloads never
miss because the run is not yet on the server.
"""

from datetime import datetime, timedelta, timezone

PUBLICATION_DELAY = timedelta(hours=6)
RUN_HOURS = (0, 6, 12, 18)


def latest_available_run(now: datetime | None = None) -> datetime:
    now = now or datetime.now(tz=timezone.utc)
    candidates = []
    for offset_days in range(2):
        base = (now - timedelta(days=offset_days)).replace(minute=0, second=0, microsecond=0)
        for hour in RUN_HOURS:
            candidate = base.replace(hour=hour)
            if now - candidate >= PUBLICATION_DELAY:
                candidates.append(candidate)
    return max(candidates)
