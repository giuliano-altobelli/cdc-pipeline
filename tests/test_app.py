from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from src.cdc_logical_replication import app


class _StubLeaderSession:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


def _run_settings() -> SimpleNamespace:
    return SimpleNamespace(
        replication_slot="slot_a",
        kinesis_stream="stream_a",
        leader_lock_key=42,
        postgres_conninfo="postgres://example",
        output_plugin="wal2json",
        standby_retry_interval_s=0.01,
    )


def _pipeline_settings() -> SimpleNamespace:
    return SimpleNamespace(
        inflight_max_messages=100,
        inflight_max_bytes=1_000_000,
        aws_region="us-east-1",
        kinesis_stream="stream_a",
        kinesis_batch_max_records=100,
        kinesis_batch_max_bytes=1_000_000,
        kinesis_batch_max_delay_ms=10,
        kinesis_retry_base_delay_ms=1,
        kinesis_retry_max_delay_ms=5,
        kinesis_retry_max_attempts=3,
    )


def test_run_passes_resolved_slot_lsn_to_leader_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        settings = _run_settings()
        leader_session = _StubLeaderSession()
        ensured = 0
        looked_up = 0
        pipeline_lsn: int | None = None

        monkeypatch.setattr(app, "Settings", lambda: settings)

        async def fake_wait_for_leadership(
            *,
            conninfo: str,
            lock_key: int,
            retry_interval_s: float,
        ) -> _StubLeaderSession:
            assert conninfo == settings.postgres_conninfo
            assert lock_key == settings.leader_lock_key
            assert retry_interval_s == settings.standby_retry_interval_s
            return leader_session

        async def fake_ensure_replication_slot(
            *,
            conninfo: str,
            slot_name: str,
            output_plugin: str,
        ) -> bool:
            nonlocal ensured
            ensured += 1
            assert conninfo == settings.postgres_conninfo
            assert slot_name == settings.replication_slot
            assert output_plugin == settings.output_plugin
            return False

        async def fake_get_replication_slot_confirmed_lsn(
            *,
            conninfo: str,
            slot_name: str,
        ) -> int:
            nonlocal looked_up
            looked_up += 1
            assert conninfo == settings.postgres_conninfo
            assert slot_name == settings.replication_slot
            return 1234

        async def fake_run_leader_pipeline(
            *,
            settings: object,
            leader_session: object,
            initial_frontier_lsn: int,
        ) -> None:
            nonlocal pipeline_lsn
            pipeline_lsn = initial_frontier_lsn
            raise asyncio.CancelledError

        monkeypatch.setattr(app, "wait_for_leadership", fake_wait_for_leadership)
        monkeypatch.setattr(app, "ensure_replication_slot", fake_ensure_replication_slot)
        monkeypatch.setattr(
            app,
            "get_replication_slot_confirmed_lsn",
            fake_get_replication_slot_confirmed_lsn,
        )
        monkeypatch.setattr(app, "_run_leader_pipeline", fake_run_leader_pipeline)

        with pytest.raises(asyncio.CancelledError):
            await app.run()

        assert ensured == 1
        assert looked_up == 1
        assert pipeline_lsn == 1234
        assert leader_session.close_calls == 1

    asyncio.run(scenario())


def test_run_does_not_fallback_to_zero_when_slot_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        settings = _run_settings()
        leader_session = _StubLeaderSession()
        wait_calls = 0
        ensured = 0
        looked_up = 0
        pipeline_calls = 0
        logged_errors: list[str] = []

        monkeypatch.setattr(app, "Settings", lambda: settings)

        async def fake_wait_for_leadership(
            *,
            conninfo: str,
            lock_key: int,
            retry_interval_s: float,
        ) -> _StubLeaderSession:
            nonlocal wait_calls
            wait_calls += 1
            assert conninfo == settings.postgres_conninfo
            assert lock_key == settings.leader_lock_key
            assert retry_interval_s == settings.standby_retry_interval_s
            if wait_calls == 1:
                return leader_session
            raise asyncio.CancelledError

        async def fake_ensure_replication_slot(
            *,
            conninfo: str,
            slot_name: str,
            output_plugin: str,
        ) -> bool:
            nonlocal ensured
            ensured += 1
            assert conninfo == settings.postgres_conninfo
            assert slot_name == settings.replication_slot
            assert output_plugin == settings.output_plugin
            return False

        async def fake_get_replication_slot_confirmed_lsn(
            *,
            conninfo: str,
            slot_name: str,
        ) -> int:
            nonlocal looked_up
            looked_up += 1
            assert conninfo == settings.postgres_conninfo
            assert slot_name == settings.replication_slot
            raise RuntimeError("lookup failed")

        async def fake_run_leader_pipeline(
            *,
            settings: object,
            leader_session: object,
            initial_frontier_lsn: int,
        ) -> None:
            nonlocal pipeline_calls
            pipeline_calls += 1

        async def fake_sleep(_: float) -> None:
            return None

        def fake_logger_exception(message: str, *args: object, **kwargs: object) -> None:
            _ = args, kwargs
            logged_errors.append(message)

        monkeypatch.setattr(app, "wait_for_leadership", fake_wait_for_leadership)
        monkeypatch.setattr(app, "ensure_replication_slot", fake_ensure_replication_slot)
        monkeypatch.setattr(
            app,
            "get_replication_slot_confirmed_lsn",
            fake_get_replication_slot_confirmed_lsn,
        )
        monkeypatch.setattr(app, "_run_leader_pipeline", fake_run_leader_pipeline)
        monkeypatch.setattr(app.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(app.LOGGER, "exception", fake_logger_exception)

        with pytest.raises(asyncio.CancelledError):
            await app.run()

        assert wait_calls == 2
        assert ensured == 1
        assert looked_up == 1
        assert pipeline_calls == 0
        assert logged_errors == ["leader_cycle_failed"]
        assert leader_session.close_calls == 1

    asyncio.run(scenario())


def test_run_leader_pipeline_seeds_ack_tracker_with_initial_frontier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        settings = _pipeline_settings()
        leader_session = _StubLeaderSession()

        async def fake_consume_replication_stream(
            *,
            settings: object,
            queue: object,
            ack_tracker: object,
            frontier_updates: object,
        ) -> None:
            _ = settings, queue, frontier_updates
            assert getattr(ack_tracker, "frontier_lsn") == 555
            raise RuntimeError("reader stopped")

        class _StubPublisher:
            def __init__(self, **kwargs: object) -> None:
                _ = kwargs

            async def run(
                self,
                *,
                queue: object,
                ack_tracker: object,
                frontier_updates: object,
            ) -> None:
                _ = queue, ack_tracker, frontier_updates
                await asyncio.sleep(3600)

        async def fake_leadership_watchdog(
            leader_session: object,
            interval_s: float,
            stop_event: asyncio.Event,
        ) -> None:
            _ = leader_session, interval_s
            await stop_event.wait()

        monkeypatch.setattr(app, "consume_replication_stream", fake_consume_replication_stream)
        monkeypatch.setattr(app, "KinesisPublisher", _StubPublisher)
        monkeypatch.setattr(app, "create_kinesis_client", lambda *, region_name: object())
        monkeypatch.setattr(app, "leadership_watchdog", fake_leadership_watchdog)

        with pytest.raises(RuntimeError, match="reader stopped"):
            await app._run_leader_pipeline(
                settings=settings,
                leader_session=leader_session,
                initial_frontier_lsn=555,
            )

    asyncio.run(scenario())
