from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from core.cache import cache

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def isolate_shared_application_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep global caches and resource thresholds isolated between test cases."""
    cache.clear()
    for name in (
        "AMENITY_RESOURCE_INTERVAL_SECONDS",
        "AMENITY_RESOURCE_CPU_SPIKE_PERCENT",
        "AMENITY_RESOURCE_CPU_HIGH_PERCENT",
        "AMENITY_RESOURCE_CPU_HIGH_SECONDS",
        "AMENITY_RESOURCE_RSS_LIMIT_MB",
        "AMENITY_RESOURCE_RSS_SPIKE_MB",
        "AMENITY_RESOURCE_ALERT_COOLDOWN_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    try:
        yield
    finally:
        cache.clear()
