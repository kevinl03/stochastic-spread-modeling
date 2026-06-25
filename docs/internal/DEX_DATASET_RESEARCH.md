# DEX Dataset Research — CEX-Correlated Collection Plan

**Date:** June 24, 2026  
**Status:** Collector implemented on `feat/dex-dataset`; **60h production run in progress**  
**Active run:** `data/dex/20260624_101824` (started 2026-06-24 17:18 UTC)  
**Related:** `experiments/collect_dex_data.py`, CEX runs in `data/statarb/`, HF `SFU-fintech-AI/statarb-crypto-research`

---

## Goal

Collect volatile-asset DEX snapshots that **correlate with existing CEX stat-arb data** for cross-venue spread analysis (CEX↔DEX), as a **separate dataset** (not mixed into CEX JSONL).

---

## What the repo already has

`experiments/collect_dex_data.py` polls **DexScreener** (free, ~300 req/min, no API key) and writes:

| Stream | Contents |
|--------|----------|
| `dex_pools` | Per-pool price USD, liquidity, volume (1h/6h/24h), txn counts, price change, FDV, mcap, dex + chain |
| `dex_spreads` | Cross-DEX spread matrix per token (low/high venue, spread bps) |

Prior test runs: `data/dex/20260602_*` (12 tokens, 120s cadence, 2h).  
Production run `20260624_101824`: **19 tokens**, 60s cadence, 60h, `$50k` min liquidity.  
`TOKEN_ADDRESSES` expanded on `feat/dex-dataset`; DOGE/XRP/ADA/TIA remain intentionally unmapped.

### Active run parameters (`20260624_101824`)

| Field | Value |
|-------|-------|
| `run_id` | `20260624_101824` |
| Start | 2026-06-24 17:18:24 UTC |
| Cadence | 60s |
| Duration | 60h (~3,600 snapshots) |
| Min liquidity | $50,000 USD per pool |
| Tokens (19) | BTC, ETH, SOL, AVAX, CRV, LDO, UNI, AAVE, ARB, OP, PEPE, WIF, BONK, FLOKI, SHIB, WLD, SEI, SUI, ENA |
| API | DexScreener (`/token-pairs/v1/{chain}/{address}`) |
| Output streams | `dex_pools.jsonl`, `dex_spreads.jsonl`, `run_config.jsonl`, `_state.json` |

**Resume after interrupt** (preserves `start_time` and snapshot index):

```bash
python experiments/collect_dex_data.py \
  --resume data/dex/20260624_101824 \
  --interval 60 --hours 60 --min-liquidity 50000
```

Expected completion: ~2026-06-27 05:18 UTC (60h from start). Check `_state.json` → `last_snapshot` for progress.

**Post-processing — drop snapshots 0–9:** The collector was resumed at snapshot 10 after fixing the WIF `$WIF` symbol filter. Snapshots **0 through 9** are missing WIF pools and should be **excluded** from export, Parquet builds, and CEX↔DEX joins. Use `snapshot_idx >= 10` when filtering this run.

```python
# Example filter for export / analysis
df = df[df["snapshot_idx"] >= 10]
```

### Pool row schema (`dex_pools.jsonl`)

```
token, chain, dex, pair_address, base_symbol, quote_symbol,
price_usd, price_native, liquidity_usd, liquidity_base, liquidity_quote,
volume_1h/6h/24h, txns_24h_buys/sells, price_change_1h/24h,
fdv, market_cap, is_major_dex, labels, url,
snapshot, snapshot_idx, timestamp
```

### Spread row schema (`dex_spreads.jsonl`)

Cross-DEX spread per token per snapshot (requires ≥2 qualifying pools):

```
token, n_pools, low_price, low_venue, low_liquidity_usd,
high_price, high_venue, high_liquidity_usd, spread_bps,
snapshot, snapshot_idx, timestamp
```

`low_venue` / `high_venue` are `{dex}:{chain}` strings. `spread_bps` = `(high/low − 1) × 10,000`.

---

## Integration with CEX dataset and research paper

### CEX reference run

The volatile-asset CEX collector run used for cross-venue correlation testing:

| Field | Value |
|-------|-------|
| Local path | `data/statarb/20260622_031058` |
| HF publish | `SFU-fintech-AI/statarb-crypto-research` → `test/` config (Parquet export) |
| Cadence | 60s (matches DEX run) |
| Asset mode | `volatile` (`VOLATILE_COINS` from `scripts/data.py`) |

The DEX run is **not time-aligned** with the CEX window (CEX ~Jun 22, DEX ~Jun 24–27). For paper analysis:

1. **Within-venue DEX stat-arb** — use `dex_spreads` alone (cross-DEX mean-reversion, same as CEX `spread_matrix`).
2. **CEX↔DEX spread** — join on `coin` + nearest `timestamp` (±30s) or aligned `snapshot_idx` if future runs are started in parallel. Map wrapped symbols: WBTC→BTC, WETH→ETH.
3. **Shared coin set** — 15 of the proposed 16-coin CEX↔DEX set are in the DEX run (TIA omitted; FLOKI/WLD/SEI/ENA added as exploratory).

Planned join output stream: `cex_dex_spread` (CEX ticker mid vs best DEX pool per coin) — not yet implemented.

### Hugging Face layout

| Dataset | Role |
|---------|------|
| `SFU-fintech-AI/statarb-crypto-research` | Primary CEX multi-signal dataset (paper) |
| `SFU-fintech-AI/statarb-crypto-stablecoins` | Auxiliary stablecoin window (accidental resume) |
| `SFU-fintech-AI/statarb-crypto-dex` *(planned)* | Separate DEX Parquet export mirroring CEX subset layout |

Export path after collection: `experiments/export_run_to_parquet.py` → HF dataset card (same pattern as CEX `test/` publish). DEX data stays out of git (`data/dex/` in `.gitignore`).

### Paper use case (`paper/stochastic-cross-venue-ohlcv-trading.tex`)

Cross-venue spread analysis extends the paper's CEX-only stat-arb framing:

- **Hypothesis:** CEX exchange-lag edges (Crypto.com, MEXC) may correlate with DEX pool dislocations on the same assets.
- **Signals:** CEX `ticker` mid + `spread_matrix` vs DEX `dex_pools` best-liquidity price and `dex_spreads` cross-DEX matrix.
- **Limitation callout:** DEX prices exclude gas, bridge, and MEV; wide `spread_bps` on thin pools (e.g. SHIB ShibaSwap vs Uniswap) are informational, not directly tradable.

---

## Per-source data availability (~60s cadence)

| Source | Access | Price | Liquidity | Volume | Order book | Historical | Notes |
|--------|--------|-------|-----------|--------|------------|------------|-------|
| **DexScreener** | REST free | ✅ | ✅ pool USD | ✅ 1h/6h/24h | ❌ | ❌ live only | Covers Uniswap, Raydium, Orca, PancakeSwap, Curve, Aerodrome, Sushi, etc. [Docs](https://docs.dexscreener.com/api/reference) |
| **Uniswap v3** | The Graph subgraph | ✅ | ✅ + ticks | ✅ day data | ⚠️ tick liquidity | ✅ indexed | [Subgraph queries](https://developers.uniswap.org/docs/ecosystem/subgraphs/concepts/v3/queries) |
| **Jupiter** | REST + API key | ✅ execution mid | ⚠️ via quote | ❌ | ✅ quote slippage | ❌ live | Solana; [Price API v3](https://developers.jup.ag/docs/price/index.md), free tier ~1 RPS |
| **1inch / CoW** | Quote APIs | ✅ | route-dependent | ❌ | ❌ | ❌ | Execution simulation, not primary monitoring |

**Not available on DEX (vs CEX):** L2 order book, funding/OI, exchange status. Depth = pool reserves or CLMM ticks.

**Recommendation:** DexScreener as primary collector (already implemented); optional Jupiter layer for Solana execution-quality quotes on WIF/BONK/SOL.

---

## CEX overlap map (23 volatile coins)

| Tier | Coins | DEX status |
|------|-------|------------|
| **Strong overlap** | BTC (WBTC), ETH (WETH), SOL, AVAX, CRV, LDO, UNI, AAVE, ARB, OP, PEPE | In `TOKEN_ADDRESSES`; multi-chain |
| **Solana-heavy** | WIF, BONK | WIF mapped; **BONK missing** |
| **Addable** | SHIB, FLOKI, SEI, SUI, TIA, ENA, WLD | Need SPL/ERC-20 addresses |
| **Weak / skip** | DOGE, XRP, ADA | No meaningful EVM DEX liquidity |

**Proposed shared set for CEX↔DEX comparison (~16 coins):**  
`BTC, ETH, SOL, AVAX, CRV, LDO, UNI, AAVE, ARB, OP, PEPE, WIF, BONK, SHIB, SUI, TIA`

Join using wrapped symbols (WBTC↔BTC, WETH↔ETH). Match snapshots by timestamp ±30s or aligned `snapshot_idx` when collected in parallel.

---

## Proposed dataset schema

Align with CEX export for joinability:

```
run_id, ts, snapshot_idx, coin, chain, dex, pair_address,
price_usd, price_native, liquidity_usd, volume_1h/6h/24h,
txns_buys/sells, price_change_1h/24h, is_major_dex, source
```

Streams: `dex_pools`, `dex_spreads`, optional `cex_dex_spread` (computed join).

**HF target:** separate repo e.g. `SFU-fintech-AI/statarb-crypto-dex` with Parquet subsets mirroring CEX layout.

---

## Limitations

- DexScreener: no historical backfill; must collect live.
- Top ~30 pools per token; need “best pool” or VWAP aggregation policy.
- Cross-chain fragmentation (same coin, different chains = different prices).
- Gas/bridge/MEV not captured in pool price alone.
- Jupiter now requires API key for production Solana quotes.

---

## Data quality — run `20260624_101824` (early check, snapshot 7)

Validated 2026-06-24 after ~8 minutes / 8 snapshots. Compared to prior test `20260602_162504`.

> **Exclude snapshots 0–9** from final dataset (pre-WIF-fix segment; resumed at snapshot 10).

| Check | Result |
|-------|--------|
| Snapshot cadence | Stable: 229 pool rows + 14 spread rows per snapshot |
| Null required fields | None |
| Duplicate pool keys | 0 / 1,832 unique |
| Liquidity filter | All pools ≥ $50k (min observed ~$50.7k; p50 ~$370k) |
| Cross-DEX spreads | 14 tokens/snapshot; p50 ~275 bps, max ~4,800 bps (SHIB thin-pool outlier) |
| vs June 2 test | Higher token count (19 vs 12); same per-snapshot pool stability; June 2 had occasional partial snapshots (API hiccups at snaps 12, 43, 51) |

**Token coverage (pools per snapshot, snap 7):**

| Tier | Tokens | Pools/snap |
|------|--------|------------|
| Strong | BTC, ETH, SOL | 43, 67, 26 |
| Good | AVAX, UNI, AAVE, BONK | 15–15, 8 |
| Moderate | CRV, LDO, ARB, OP, SHIB, SUI | 3–11 |
| Thin | PEPE | 3 |
| Zero pools | FLOKI, WLD, SEI, ENA | 0 (weak DexScreener coverage) |

**Zero-pool tokens:**

- **FLOKI, WLD, SEI, ENA** — expected weak DexScreener coverage at $50k filter (research plan).
- **WIF** — fixed locally: strip `$` from DexScreener base symbols (e.g. `$WIF` → WIF). Resume run to pick up ~3+ Orca/Raydium pools per snapshot.

**CEX 16-coin overlap:** 15 present; TIA intentionally skipped. Extras in DEX run: FLOKI, WLD, SEI, ENA.

---

## Implementation plan (ordered)

1. ~~**Expand `TOKEN_ADDRESSES`** for the 16-coin shared set; wire `VOLATILE_COINS` from `scripts/data.py`.~~ ✅ `feat/dex-dataset`
2. ~~**Collector hardening:** `fsync`, 60s cadence, crash-safe `_state.json`.~~ ✅
3. **60h collection run** — **in progress** (`20260624_101824`).
4. ~~**Fix WIF `$WIF` symbol filter**~~ — done locally; resume run to backfill WIF pools.
5. **`cex_dex_spread` join script** — CEX ticker mid vs best DEX pool per coin.
6. **Export to Parquet + HF** (`statarb-crypto-dex` dataset card).
