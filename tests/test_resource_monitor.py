from __future__ import annotations

import asyncio
import builtins
import io
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest

import api.log as log_module
import core.resource_monitor as resource_monitor_module
from core.amenity import Amenity
from core.resource_monitor import ResourceMonitor

if TYPE_CHECKING:
    from collections.abc import Iterator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIB = 1024 * 1024


@pytest.fixture
def bot() -> Iterator[Amenity]:
    instance = Amenity()
    try:
        yield instance
    finally:
        instance._blocking_executor.shutdown(wait=True)


@pytest.fixture
def webhook_spy(monkeypatch: pytest.MonkeyPatch) -> tuple[list[object], AsyncMock]:
    scheduled: list[object] = []
    sender = AsyncMock(return_value=True)

    def schedule(coroutine: object, _loop: object) -> SimpleNamespace:
        scheduled.append(coroutine)
        return SimpleNamespace(add_done_callback=Mock())

    monkeypatch.setattr(resource_monitor_module, "log_resource_alert", sender)
    monkeypatch.setattr(resource_monitor_module.asyncio, "run_coroutine_threadsafe", schedule)
    return scheduled, sender


def test_rss_reader_uses_current_resident_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_open(path: str, *, encoding: str) -> io.StringIO:
        assert path == "/proc/self/statm"
        assert encoding == "ascii"
        return io.StringIO("100 12 0 0 0 0 0")

    monkeypatch.setattr(builtins, "open", fake_open)
    monkeypatch.setattr(resource_monitor_module.os, "sysconf", lambda _name: 4096)

    assert resource_monitor_module._process_rss_bytes() == 12 * 4096


def test_invalid_environment_threshold_falls_back(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_RESOURCE_LIMIT", "not-a-number")
    assert resource_monitor_module._env_float("TEST_RESOURCE_LIMIT", 12.0) == 12.0
    assert "Invalid TEST_RESOURCE_LIMIT" in caplog.text


@pytest.mark.asyncio
async def test_monitor_reads_dotenv_thresholds_when_started(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AMENITY_RESOURCE_INTERVAL_SECONDS", raising=False)
    monitor = ResourceMonitor(tmp_path)
    assert monitor._interval == 1.0

    monkeypatch.setenv("AMENITY_RESOURCE_INTERVAL_SECONDS", "0.25")
    started = threading.Event()
    monkeypatch.setattr(monitor, "_sample_resources", started.set)
    monitor.start(asyncio.get_running_loop())
    try:
        assert await asyncio.to_thread(started.wait, 1)
        assert monitor._interval == 0.25
    finally:
        await monitor.stop()


def test_stack_sampler_reports_project_function_and_line() -> None:
    monitor = ResourceMonitor(PROJECT_ROOT)

    stacks = monitor._sample_python_stacks()

    assert any("test_stack_sampler_reports_project_function_and_line" in stack for _, stack in stacks)
    assert any("tests/test_resource_monitor.py:" in stack for _, stack in stacks)


def test_cpu_spike_and_sustained_thresholds_are_distinguished() -> None:
    spike_monitor = ResourceMonitor(PROJECT_ROOT)
    spike_monitor._report = Mock()  # type: ignore[method-assign]
    spike_monitor._check_cpu(spike_monitor._cpu_spike_percent + 1, 100.0, 100 * MIB, [])
    spike_calls = spike_monitor._report.call_args_list

    sustained_monitor = ResourceMonitor(PROJECT_ROOT)
    sustained_monitor._report = Mock()  # type: ignore[method-assign]
    sustained_cpu = min(
        sustained_monitor._cpu_high_percent + 1,
        sustained_monitor._cpu_spike_percent - 1,
    )
    sustained_monitor._check_cpu(sustained_cpu, 200.0, 100 * MIB, [])
    sustained_monitor._check_cpu(
        sustained_cpu,
        200.0 + sustained_monitor._cpu_high_seconds,
        100 * MIB,
        [],
    )
    sustained_calls = sustained_monitor._report.call_args_list

    assert len(spike_calls) == 1
    assert spike_calls[0].args[0] == "cpu"
    assert "CPU spike" in spike_calls[0].args[1]
    assert len(sustained_calls) == 1
    assert sustained_calls[0].args[0] == "cpu"
    assert "Sustained high CPU" in sustained_calls[0].args[1]


def test_memory_limit_and_growth_have_separate_reasons() -> None:
    monitor = ResourceMonitor(PROJECT_ROOT)
    monitor._rss_limit_bytes = 512 * MIB
    monitor._rss_spike_bytes = 128 * MIB
    monitor._report = Mock()  # type: ignore[method-assign]

    monitor._check_rss(520 * MIB, 510 * MIB, 10_000.0, 2.0, [])
    monitor._check_rss(300 * MIB, 100 * MIB, 10_001.0, 2.0, [])

    calls = monitor._report.call_args_list
    assert [call.args[0] for call in calls] == ["rss-high", "rss-spike"]
    assert "configured" in calls[0].args[2]
    assert "increased by" in calls[1].args[2]


@pytest.mark.asyncio
async def test_resource_alert_uses_error_webhook_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    error_webhook = AsyncMock(return_value=True)
    monkeypatch.setattr(log_module, "ErrorWebhook", error_webhook)

    sent = await log_module.log_resource_alert("CPU spike detected", "A sampled function was hot.")

    assert sent
    error_webhook.assert_awaited_once()
    embed = error_webhook.await_args.args[0]
    assert embed.title == "CPU spike detected"
    assert embed.description == "A sampled function was hot."
    assert embed.color.value == log_module.discord.Color.red().value


@pytest.mark.asyncio
async def test_alert_includes_command_stack_metrics_and_respects_cooldown(
    tmp_path: Path,
    webhook_spy: tuple[list[object], AsyncMock],
) -> None:
    scheduled, sender = webhook_spy
    monitor = ResourceMonitor(tmp_path)
    monitor._loop = SimpleNamespace(is_closed=lambda: False)  # type: ignore[assignment]
    now = time.monotonic()

    with monitor.track_command("slash: /lookup repo"):
        monitor._report(
            "cpu",
            "CPU spike detected",
            "Process CPU reached 205%.",
            205.0,
            200 * MIB,
            [("amenity-cpu_0", "cogs/tools.py:99 in lookup_repo()")],
            now,
        )

    monitor._report(
        "cpu",
        "CPU spike detected",
        "Repeated sample.",
        210.0,
        201 * MIB,
        [],
        now + 1,
    )
    assert len(scheduled) == 1
    await scheduled[0]  # type: ignore[misc]

    sender.assert_awaited_once()
    title, description = sender.await_args.args
    assert title == "CPU spike detected"
    assert "slash: /lookup repo" in description
    assert "cogs/tools.py:99 in lookup_repo()" in description
    assert "205%" in description
    assert "200 MiB" in description


@pytest.mark.asyncio
async def test_recent_command_is_retained_for_transient_spike(
    tmp_path: Path,
    webhook_spy: tuple[list[object], AsyncMock],
) -> None:
    scheduled, sender = webhook_spy
    monitor = ResourceMonitor(tmp_path)
    monitor._loop = SimpleNamespace(is_closed=lambda: False)  # type: ignore[assignment]

    with monitor.track_command("prefix: image avatar"):
        pass

    monitor._report(
        "rss-spike",
        "Memory spike detected",
        "Rapid growth.",
        5.0,
        300 * MIB,
        [],
        time.monotonic(),
    )
    await scheduled[0]  # type: ignore[misc]

    assert "prefix: image avatar" in sender.await_args.args[1]
    assert "Recently completed command(s, last 10s)" in sender.await_args.args[1]


@pytest.mark.asyncio
async def test_prefix_invocation_tracks_command_until_completion(
    bot: Amenity,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_parent_invoke(_bot: object, _context: object) -> None:
        assert bot._resource_monitor._active_commands["prefix: ping"] == 1

    monkeypatch.setattr("discord.ext.commands.Bot.invoke", fake_parent_invoke)
    context = SimpleNamespace(command=SimpleNamespace(qualified_name="ping"))

    await bot.invoke(context)  # type: ignore[arg-type]

    active, recent = bot._resource_monitor._command_names(time.monotonic())
    assert active == []
    assert recent == ["prefix: ping"]


@pytest.mark.asyncio
async def test_slash_dispatch_tracks_nested_command_name(
    bot: Amenity,
) -> None:
    observed: list[str] = []

    async def dispatch(interaction: object) -> None:
        del interaction
        observed.extend(bot._resource_monitor._active_commands)

    bot.tree._call = dispatch  # type: ignore[method-assign]
    bot._instrument_app_command_dispatch()
    interaction = SimpleNamespace(
        data={
            "name": "lookup",
            "options": [{"name": "repo", "type": 1, "options": []}],
        }
    )

    await bot.tree._call(interaction)  # type: ignore[arg-type]

    assert observed == ["slash: /lookup repo"]
    assert bot._resource_monitor._command_names(time.monotonic())[0] == []
