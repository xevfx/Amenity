# Test guide

The tests run without a Discord bot token or external API credentials. Network, Discord, and webhook calls in the service regression tests are replaced with local fakes.

## Suites

- `test_command_workflow.py` exercises representative offline commands, checks that the command catalog stays complete, and records timing and memory metrics in `artifacts/command-workflow-report.json`.
- `test_service_regressions.py` covers HTTP response handling, cancellation, shared user lookup caching, and the GitHub, crypto, and Tavily cache behavior.
- `test_concurrency_safety.py` covers cache single-flight behavior, bounded worker pools, CPU offloading, database update serialization, and process-safe usage writes.
- `test_event_loop_safety.py` checks that blocking maintenance work stays off the gateway loop.
- `test_resource_monitor.py` covers resource thresholds, webhook payloads, command attribution, and mention/slash dispatch tracking.
- `test_owner_broadcast.py` covers broadcast confirmation and delivery outcomes.

`conftest.py` clears the shared application cache and resource-monitor environment settings between tests so results do not depend on previous cases or deployment configuration.

## Run

Run the full suite with `pytest -q`. Run one area with, for example, `pytest -q tests/test_service_regressions.py tests/test_resource_monitor.py`.

The command workflow timing limits can be adjusted with `COMMAND_TEST_MAX_SECONDS` and `COMMAND_TEST_MAX_RSS_DELTA_MB` when comparing machines with different performance limits.
