# DEX Dataset Research — CEX-Correlated Collection Plan

**Date:** June 24, 2026  
**Status:** Research complete; implementation pending (Step 1: expand `TOKEN_ADDRESSES`)  
**Related:** `experiments/collect_dex_data.py`, CEX runs in `data/statarb/`

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

Prior test runs: `data/dex/20260602_*`. Only **14 of 23** `VOLATILE_COINS` have `TOKEN_ADDRESSES`; DOGE/XRP/ADA are intentionally empty.

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

## Implementation plan (ordered)

1. **Expand `TOKEN_ADDRESSES`** for the 16-coin shared set; wire `VOLATILE_COINS` from `scripts/data.py`.
2. **Collector hardening:** day-partitioned JSONL, `fsync`, 60s cadence, match CEX collector patterns.
3. **60h collection run** (parallel or offset from next CEX window).
4. **`cex_dex_spread` join script** — CEX ticker mid vs best DEX pool per coin.
5. **Export to Parquet + HF** (separate dataset card).

Step 1 is approved; code changes land on `feat/dex-dataset` via PR before merge.
