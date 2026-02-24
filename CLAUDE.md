# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Postgres logical replication to Kinesis micro-batching service. Reads WAL changes via `wal2json`, queues them in a bounded in-memory queue, micro-batches by time/size/count, and publishes to AWS Kinesis Data Streams. Uses Postgres advisory locks for leader election so only one instance processes at a time.

## Commands

```bash
uv sync --group dev              # Install runtime + dev dependencies
uv run ruff check src tests      # Lint (pyflakes, imports, pycodestyle)
uv run ruff format               # Auto-format
uv run pyrefly check             # Type checking
uv run pytest -q tests           # Run unit tests
uv run pytest -q tests/test_ack_tracker.py              # Run single test file
uv run pytest -q tests/test_ack_tracker.py::test_name   # Run single test
RUN_INTEGRATION_TESTS=1 uv run pytest -q -m integration # Integration tests (needs AWS/Postgres)
uv run cdc-logical-replication   # Start the service
```

Infrastructure: `cd infra/stacks/cdc_logical_replication_dev && terraform init && terraform apply`

## Architecture

Three concurrent async tasks orchestrated in `app.py`:
1. **replication_reader** (`replication.py`) — consumes Postgres logical replication frames, parses wal2json, registers events with AckTracker, pushes to InflightEventQueue
2. **kinesis_publisher** (`kinesis.py`) — drains queue via MicroBatcher (`batching.py`), publishes batches to Kinesis with exponential backoff retries, reports acks back to AckTracker
3. **leader_watchdog** (`leader.py`) — polls advisory lock; raises on leadership loss to trigger failover

Key data flow: `Postgres WAL → replication.py → InflightEventQueue → batching.py → kinesis.py → Kinesis`

### Core Modules (`src/cdc_logical_replication/`)

| Module | Role |
|---|---|
| `settings.py` | Pydantic-based config from env vars (Postgres, Kinesis, batching, queue, leadership) |
| `ack.py` | Tracks highest contiguous LSN published (frontier); only advances when all prior LSNs are acked |
| `queue.py` | Bounded async queue (max messages + max bytes) |
| `models.py` | `ChangeEvent` dataclass (lsn, ack_id, payload, partition_key) |
| `partition_key.py` | Extracts Kinesis partition key (primary_key mode or fallback to lsn/table/static) |
| `protocol.py` | Binary protocol parsing/building (XLogData, Keepalive, Standby Status) |
| `slot.py` | Replication slot creation and LSN checkpoint queries |
| `json_logging.py` | JSON log formatter; extracts `extra` fields into structured output |

### Design Decisions

- **Contiguous frontier tracking**: AckTracker only advances the confirmed LSN when all earlier events are published, handling out-of-order completion
- **Dropped events advance frontier**: Non-retriable failures (oversized, access denied) drop the event but still advance the frontier — trades durability for liveness
- **Error classification in Kinesis**: Retriable errors (throttling, network) get exponential backoff; non-retriable errors (validation, access) fail fast

## Code Style

- Python 3.10+, 100-char line length, 4-space indent, explicit type hints
- `snake_case` functions/vars, `PascalCase` classes, `UPPER_SNAKE_CASE` constants
- Structured logging: `LOGGER.info("event_name", extra={...})` — JSON output by default (`JsonLogFormatter`), plain text with `LOG_FORMAT=plain`
- Imports grouped: stdlib → third-party → local
- All I/O is async (asyncio)

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_LEVEL` | `INFO` | Python log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `LOG_FORMAT` | `json` | Log output format: `json` (structured) or `plain` (human-readable) |

## Testing

- Unit tests in `tests/test_*.py`, integration tests in `tests/integration/`
- Integration tests gated by `RUN_INTEGRATION_TESTS=1` env var and `@pytest.mark.integration`
- When modifying replication/publisher paths, add coverage for retries, ack/frontier progression, and queue drain behavior
