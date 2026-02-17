# Repository Guidelines

## Project Structure & Module Organization
Core service code lives in `src/cdc_logical_replication/` (replication, queueing, Kinesis publishing, leadership, and settings).
Tests are in `tests/`, with live infrastructure tests in `tests/integration/`.
Infrastructure code is under `infra/`:
- `infra/modules/`: reusable Terraform modules (`kinesis_stream`, `rds_postgres_logical_replication`)
- `infra/stacks/cdc_logical_replication_dev/`: developer stack wiring and DB bootstrap script

Primary project config is in `pyproject.toml`; dependency locks are in `uv.lock`.

## Build, Test, and Development Commands
- `uv sync --group dev`: install runtime + dev dependencies into `.venv`.
- `uv run ruff check src tests`: run lint checks (pyflakes + import sorting + core pycodestyle errors).
- `uv run pyrefly check`: run static type checking for package code in `src/`.
- `uv run pyrefly lsp`: start the Pyrefly language server for editor integration.
- `uv run pytest -q tests`: run the test suite (unit + default-selected tests).
- `RUN_INTEGRATION_TESTS=1 uv run pytest -q -m integration`: run integration tests (requires AWS/Postgres env vars).
- `uv run cdc-logical-replication`: start the service through the package entrypoint.
- `cd infra/stacks/cdc_logical_replication_dev && terraform init && terraform apply`: provision dev AWS/Postgres/Kinesis infrastructure.

## Coding Style & Naming Conventions
Use Python 3.10+ with 4-space indentation and explicit type hints (aligned with existing code).
Naming conventions:
- modules/functions/variables: `snake_case`
- classes: `PascalCase`
- constants: `UPPER_SNAKE_CASE`

Prefer small, composable async units and structured logging (`LOGGER.info("event_name", extra={...})`).
Lint with Ruff and type-check with Pyrefly (`uv run ruff check src tests`, `uv run pyrefly check`) before opening a PR.
Keep imports grouped (stdlib, third-party, local).

## Testing Guidelines
Use `pytest` with `test_*.py` files and `test_*` function names.
Mark infra-dependent tests with `pytest.mark.integration` and gate them behind `RUN_INTEGRATION_TESTS=1`.
When touching replication/publisher paths, add regression coverage for retries, ack/frontier progression, and queue drain behavior.

## Commit & Pull Request Guidelines
Git history is currently minimal, so use clear imperative commit subjects (example: `Add bounded retry delay for Kinesis publish`) and keep commits atomic.
For PRs, include:
- what changed and why
- behavior/risk impact
- exact test commands run
- related issue/ticket (if available)
- notes or snippets for infra/config changes

## Security & Configuration Tips
Never commit secrets, Terraform state, or generated credentials.
Use least-privilege AWS credentials and tight `allowed_cidrs` (prefer `/32`) in `terraform.tfvars`.

## Agent Execution Note
In this environment, `uv` commands do not run successfully inside the sandbox.
When Codex needs to run `uv` (for example `uv sync` or `uv run pytest`), execute it with outside-sandbox permissions.
