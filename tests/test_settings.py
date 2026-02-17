from __future__ import annotations

import pytest
from pydantic import ValidationError
from src.cdc_logical_replication.settings import Settings


@pytest.fixture()
def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGHOST", "localhost")
    monkeypatch.setenv("PGPORT", "5432")
    monkeypatch.setenv("PGUSER", "postgres")
    monkeypatch.setenv("PGPASSWORD", "postgres")
    monkeypatch.setenv("PGDATABASE", "cdc")
    monkeypatch.setenv("AWS_REGION", "us-west-1")
    monkeypatch.setenv("KINESIS_STREAM", "cdc-stream")


def test_wal2json_format_version_defaults_to_2(_base_env: None) -> None:
    settings = Settings()

    assert settings.wal2json_format_version == 2
    assert "\"format-version\" '2'" in settings.wal2json_options_sql


def test_wal2json_format_version_rejects_non_2(
    _base_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WAL2JSON_FORMAT_VERSION", "1")

    with pytest.raises(ValidationError):
        Settings()


def test_kinesis_retry_max_attempts_must_be_positive(
    _base_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KINESIS_RETRY_MAX_ATTEMPTS", "0")

    with pytest.raises(ValidationError):
        Settings()
