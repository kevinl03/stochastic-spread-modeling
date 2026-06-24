# PostgreSQL Storage for Collector Data

Use PostgreSQL for queryable storage while keeping JSONL run files as raw source.

## 1) Install dependency

```bash
pip install -r requirements.txt
```

(`requirements.txt` now includes `psycopg[binary]`.)

## 2) Create a database

Example local DB:

```sql
CREATE DATABASE statarb;
```

## 3) Load a run into Postgres

```bash
python -m experiments.load_statarb_to_postgres ^
  --run-dir data/statarb/20260613_234819 ^
  --db-url postgresql://postgres:postgres@localhost:5432/statarb
```

Optional:

- `--schema statarb_raw` (default)
- `--signals ticker orderbook trades spread_matrix`
- `--batch-size 1000`

## 4) Table layout

The loader creates one table per signal (for example `statarb_raw.ticker`,
`statarb_raw.orderbook`, etc.) with common columns:

- `run_id`, `ts`, `snapshot_idx`
- `exchange`, `coin`, `market`, `symbol`
- `error`
- `payload` (`JSONB`) containing the full original record
- `inserted_at`

This keeps ingestion simple while preserving every raw field.

## 5) Example queries

Latest snapshot count:

```sql
SELECT max(snapshot_idx) AS latest_snapshot
FROM statarb_raw.ticker
WHERE run_id = '20260613_234819';
```

Ticker error rate for a run:

```sql
SELECT
  count(*) AS total,
  count(*) FILTER (WHERE error IS NOT NULL) AS errors,
  100.0 * count(*) FILTER (WHERE error IS NOT NULL) / nullif(count(*), 0) AS error_pct
FROM statarb_raw.ticker
WHERE run_id = '20260613_234819';
```

Cross-venue spread samples:

```sql
SELECT ts, coin, payload->>'total_spread_bps' AS total_spread_bps
FROM statarb_raw.spread_matrix
WHERE run_id = '20260613_234819'
ORDER BY ts DESC
LIMIT 20;
```
