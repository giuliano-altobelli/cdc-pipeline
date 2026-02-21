# CDC Ack Regression Debug Summary and Prod Checklist

## Session Summary

- Traced warning in code: `ack_register_lsn_regression` is emitted when `incoming_lsn < last_registered_lsn` (`src/cdc_logical_replication/ack.py:43`).
- Confirmed behavior is tolerated by design (warning only, non-fatal), with explicit regression test coverage.
- Ran targeted tests (non-mutating):
  - `uv run pytest -q tests/test_replication.py tests/test_ack_tracker.py tests/test_app.py`
  - Result: `17 passed`.
- Reviewed DB evidence:
  - Stable slot replication PID (`active_pid=10000`)
  - Stable advisory-lock session PID (`10034`)
  - `confirmed_flush_lsn` advanced over time
  - No evidence of connection or leadership churn in snapshots

## What This Warning Means

`ack_register_lsn_regression` means the app saw a new message with LSN lower than the highest LSN it previously registered in-process.

It does **not** automatically mean disconnection, failover, or data loss.

## Observed Data Summary and Interpretation

The following lag snapshots were captured during investigation:

| ts_utc | confirmed_flush_lsn | restart_lsn | current_wal_lsn | bytes_behind_confirmed | bytes_between_confirmed_and_restart |
| --- | --- | --- | --- | ---: | ---: |
| 2026-02-20 22:51:00.000001 | 0/1AC579B0 | 0/1AC4B170 | 0/1AC57DF8 | 1,096 | 51,264 |
| 2026-02-20 22:52:00.000001 | 0/1AC579B0 | 0/1AC4B170 | 0/1AC59338 | 6,536 | 51,264 |
| 2026-02-20 22:53:24.000001 | 0/1AC5AB18 | 0/1AC58ED0 | 0/1AC5F1D0 | 18,104 | 7,240 |
| 2026-02-20 23:35:00.000001 | 0/1AC9FC38 | 0/1AC71378 | 0/1ACEFA68 | 327,216 | 190,656 |

What this indicates:

- `confirmed_flush_lsn` advanced over time. This indicates replication feedback/checkpoint progression is working.
- `restart_lsn` also advanced. This indicates slot retention state is moving forward (not stuck).
- `bytes_behind_confirmed` stayed relatively small in early snapshots and increased later; this can happen with burstier activity and does not alone indicate failure.
- `bytes_between_confirmed_and_restart` fluctuated and later increased; that is a pressure signal to monitor, but not by itself evidence of disconnect or leader instability.

Metric definitions:

- `bytes_behind_confirmed`:
  - Formula: `pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)`
  - Meaning: how far the current WAL head is ahead of what the consumer has acknowledged safe.
  - Operationally: sustained growth without recovery suggests the consumer is falling behind.

- `bytes_between_confirmed_and_restart`:
  - Formula: `pg_wal_lsn_diff(confirmed_flush_lsn, restart_lsn)`
  - Meaning: distance between consumer-confirmed progress and the slot restart position.
  - Operationally: larger values can reflect retained WAL/decoder backlog pressure; trend and persistence matter more than a single point.

## SQL Queries and What To Look For

### 1. Slot State Over Time (run every 60s)

```sql
SELECT now() AT TIME ZONE 'utc' AS ts_utc,
       slot_name, plugin, slot_type, active, active_pid,
       confirmed_flush_lsn, restart_lsn, xmin, catalog_xmin
FROM pg_replication_slots
WHERE slot_name = '<REPLICATION_SLOT>';
```

What to look for:

- `active` should stay `true` during steady leadership.
- `active_pid` should be stable; frequent changes suggest reconnect/restart cycles.
- `confirmed_flush_lsn` should be monotonic non-decreasing.
- `restart_lsn` can move differently; that alone is not a fault.
- `confirmed_flush_lsn` stalling for long periods under active writes suggests feedback/frontier lag.

### 2. Slot Lag Context (run every 60s)

```sql
SELECT now() AT TIME ZONE 'utc' AS ts_utc,
       confirmed_flush_lsn,
       restart_lsn,
       pg_current_wal_lsn() AS current_wal_lsn,
       pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn) AS bytes_behind_confirmed,
       pg_wal_lsn_diff(confirmed_flush_lsn, restart_lsn) AS bytes_between_confirmed_and_restart
FROM pg_replication_slots
WHERE slot_name = '<REPLICATION_SLOT>';
```

What to look for:

- `bytes_behind_confirmed`: WAL distance from current head to acknowledged point.
  - Small/moderate and fluctuating is normal.
  - Persistent growth without recovery indicates lag.
- `bytes_between_confirmed_and_restart`:
  - Varies with decoding/retention behavior.
  - Sudden growth with no recovery can indicate backlog pressure.

### 3. Active Replication Sender Identity

```sql
SELECT now() AT TIME ZONE 'utc' AS ts_utc,
       pid, usename, application_name, client_addr, state,
       backend_start, state_change
FROM pg_stat_activity
WHERE backend_type = 'walsender'
   OR application_name ILIKE '%wal%'
ORDER BY backend_start DESC;
```

What to look for:

- Exactly one expected sender for your slot/leader.
- Stable PID and state over time.
- Frequent PID churn implies reconnects or pod instability.

### 4. Advisory Lock Holders (leader lock)

```sql
SELECT now() AT TIME ZONE 'utc' AS ts_utc,
       a.pid, a.usename, a.application_name, a.client_addr,
       l.granted, l.classid, l.objid, l.objsubid
FROM pg_locks l
JOIN pg_stat_activity a ON a.pid = l.pid
WHERE l.locktype = 'advisory' AND l.granted
ORDER BY a.backend_start DESC;
```

What to look for:

- Single expected lock holder pattern.
- If multiple unexpected holders appear, investigate lock-key/config drift.
- With `LEADER_LOCK_KEY_OVERRIDE` unset, lock key derives from `REPLICATION_SLOT`; inconsistent slot names can create independent leaders.

### 5. Combined Snapshot (slot + sender + advisory)

```sql
WITH slot AS (
  SELECT now() AT TIME ZONE 'utc' AS ts_utc,
         slot_name, plugin, slot_type, active, active_pid,
         confirmed_flush_lsn, restart_lsn, xmin, catalog_xmin
  FROM pg_replication_slots
  WHERE slot_name = '<REPLICATION_SLOT>'
),
sender AS (
  SELECT pid, usename, application_name, client_addr, state,
         backend_start, state_change, query
  FROM pg_stat_activity
  WHERE pid = (SELECT active_pid FROM slot)
),
advisory AS (
  SELECT a.pid, a.usename, a.application_name, a.client_addr,
         l.classid, l.objid, l.objsubid, l.granted
  FROM pg_locks l
  JOIN pg_stat_activity a ON a.pid = l.pid
  WHERE l.locktype = 'advisory' AND l.granted
)
SELECT 'slot' AS section, to_jsonb(slot.*) AS data FROM slot
UNION ALL
SELECT 'sender', to_jsonb(sender.*) FROM sender
UNION ALL
SELECT 'advisory_lock_holders', to_jsonb(advisory.*) FROM advisory;
```

What to look for:

- `slot.active_pid` must match one sender PID.
- Advisory-lock PID can differ from sender PID (expected in this app), but both should be stable.

## Before Prod

1. Run burst-focused load tests (not just daily average).
2. Set CPU/memory requests and limits to reduce throttling on small nodes.
3. Configure HPA and validate queue/lag recovery.
4. Alert on:
   - `leader_cycle_failed`, `leadership_lost`
   - `kinesis_*dropped*`, retry exhaustion
   - sustained lag (`bytes_behind_confirmed`)
   - pod restarts/OOMs
5. Treat `ack_register_lsn_regression` as warning-level; page only on abnormal rate spikes.
6. Ensure downstream idempotency/dedup for potential duplicate windows.
7. Validate Kinesis shard capacity for bursts.
8. Use safe rollout controls (PDB, anti-affinity, conservative deploy strategy).
9. Perform 24-72h prod-like soak test before cutover.

## Prod CPU/Memory Requests and Limits (to reduce throttling)

Use this as a starting point:

```yaml
resources:
  requests:
    cpu: "500m"
    memory: "1Gi"
  limits:
    cpu: "1000m"
    memory: "2Gi"
```

What to watch and tune:

- CPU throttling:
  - Monitor `container_cpu_cfs_throttled_seconds_total` and throttled periods ratio.
  - If throttling is sustained, increase CPU request first, then limit.
- Memory pressure:
  - Monitor working set vs limit and OOM kill events.
  - If working set consistently >70-80% of limit, raise memory limit/request.
- Lag coupling:
  - Correlate throttling spikes with `bytes_behind_confirmed` growth.
  - If lag grows during throttling, resource starvation is impacting feedback/publish cadence.

Right-sizing strategy:

1. Start with the profile above.
2. Run burst load test.
3. Raise requests until throttling is minimal under expected peak.
4. Keep limit about 1.5-2x request for safe burst headroom.

Recommended guardrails:

- Avoid very low CPU requests (for example `<=100m`) in prod for this workload.
- Prefer `Guaranteed` QoS for critical environments (`requests == limits`) if nodes can support it.
- Combine with HPA so replicas scale before prolonged lag accumulates.
