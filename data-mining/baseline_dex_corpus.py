"""DEX baseline corpus via the Uniswap V3 subgraph.

For each token in events_dex_baseline.csv the script:
  1. Finds the deepest Uniswap V3 pool paired with WETH (by TVL).
  2. Reads the pool's creation timestamp.
  3. Samples pool state at 12 log-spaced days after creation:
     day 1, 3, 7, 14, 21, 30, 45, 60, 90, 120, 150, 180.
  4. Computes an approximate depth-at-1% and depth-at-5% price impact.
  5. Writes results to data/baseline/dex_token_days.csv.

The depth math uses the constant-liquidity Uniswap V3 formula:
    Δx = L * (1/√P_new - 1/√P_curr)
This holds while the price range does not cross tick boundaries. For fresh
pools with concentrated liquidity the approximation is decent at 1% impact
and rougher at 5%. Full tick-walking is a follow-up iteration.

Usage:
    python baseline_dex_corpus.py --list      # print catalogue and exit
    python baseline_dex_corpus.py --discover  # populate pool addresses only
    python baseline_dex_corpus.py             # full sample + compute

Environment:
    UNISWAP_SUBGRAPH_URL  URL of the Uniswap V3 subgraph.
                          Default: The Graph legacy endpoint.
                          For production access, sign up at
                          https://thegraph.com/studio/ (free tier: 100k
                          queries/month), then use the gateway URL:
                          https://gateway.thegraph.com/api/{KEY}/subgraphs/id/{ID}
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

import config as C


DEX_CSV = C.ROOT / "events_dex_baseline.csv"
OUT_DIR = C.DATA_DIR / "baseline"
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_CSV = OUT_DIR / "dex_token_days.csv"
POOLS_CSV = OUT_DIR / "dex_pools_discovered.csv"

SUBGRAPH_URL = os.environ.get(
    "UNISWAP_SUBGRAPH_URL",
    # Uniswap V3 Ethereum mainnet on The Graph decentralized network.
    # Requires an API key from https://thegraph.com/studio/ (free tier).
    # Either set UNISWAP_SUBGRAPH_URL env var with your key, or replace
    # {API_KEY} below.
    "https://gateway.thegraph.com/api/{API_KEY}/subgraphs/id/5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV",
)

WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"

SAMPLE_DAYS = [1, 3, 7, 14, 21, 30, 45, 60, 90, 120, 150, 180]

REQUEST_TIMEOUT = 30
SLEEP_BETWEEN_QUERIES = 0.3


# ---------------------------------------------------------------------------
# GraphQL wrapper
# ---------------------------------------------------------------------------
def graphql(query: str, variables: dict | None = None,
            retries: int = 2) -> dict:
    payload = {"query": query, "variables": variables or {}}
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(SUBGRAPH_URL, json=payload,
                              timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            body = r.json()
            if "errors" in body:
                raise RuntimeError(f"GraphQL errors: {body['errors']}")
            return body.get("data") or {}
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise
    raise RuntimeError(f"unreachable: {last_exc}")


# ---------------------------------------------------------------------------
# Pool discovery
# ---------------------------------------------------------------------------
def find_pool(token_address: str, quote_address: str = WETH) -> dict | None:
    """Find the highest-TVL Uniswap V3 pool for a token/WETH pair."""
    query = """
    query FindPool($t: String!, $q: String!) {
      pools(
        where: {
          and: [
            { or: [
              {token0: $t, token1: $q},
              {token0: $q, token1: $t}
            ]}
          ]
        }
        orderBy: totalValueLockedUSD
        orderDirection: desc
        first: 3
      ) {
        id
        createdAtTimestamp
        feeTier
        totalValueLockedUSD
        token0 { id symbol decimals }
        token1 { id symbol decimals }
      }
    }
    """
    data = graphql(query, {"t": token_address.lower(),
                           "q": quote_address.lower()})
    pools = data.get("pools") or []
    if not pools:
        return None
    return pools[0]


def discover_all_pools() -> pd.DataFrame:
    """Iterate over the token catalogue, find the deepest pool for each."""
    corpus = pd.read_csv(DEX_CSV, dtype=str)
    rows = []
    for _, row in corpus.iterrows():
        tid = row["token_id"]
        print(f"[{tid}] {row['name']} ({row['token_address']})")
        try:
            pool = find_pool(row["token_address"])
        except Exception as exc:  # noqa: BLE001
            print(f"  failed: {exc}")
            continue
        if pool is None:
            print("  no Uniswap V3 pool found paired with WETH")
            rows.append({
                "token_id": tid, "name": row["name"], "pool_id": None,
                "fee_tier": None, "created_at": None, "tvl_usd_now": None,
                "token0_symbol": None, "token1_symbol": None,
                "token0_decimals": None, "token1_decimals": None,
            })
            continue
        created = datetime.fromtimestamp(
            int(pool["createdAtTimestamp"]), tz=timezone.utc)
        print(f"  pool {pool['id']} fee {int(pool['feeTier'])/1e4:.2f}% "
              f"created {created.date()} TVL ${float(pool['totalValueLockedUSD']):,.0f}")
        rows.append({
            "token_id": tid,
            "name": row["name"],
            "pool_id": pool["id"],
            "fee_tier": int(pool["feeTier"]),
            "created_at": created.isoformat(),
            "tvl_usd_now": float(pool["totalValueLockedUSD"]),
            "token0_symbol": pool["token0"]["symbol"],
            "token1_symbol": pool["token1"]["symbol"],
            "token0_decimals": int(pool["token0"]["decimals"]),
            "token1_decimals": int(pool["token1"]["decimals"]),
            "token0_id": pool["token0"]["id"],
            "token1_id": pool["token1"]["id"],
        })
        time.sleep(SLEEP_BETWEEN_QUERIES)
    df = pd.DataFrame(rows)
    df.to_csv(POOLS_CSV, index=False)
    print(f"\nWrote {len(df)} rows -> {POOLS_CSV}")
    return df


# ---------------------------------------------------------------------------
# Sampling pool state
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Historical block lookup via timestamp approximation
# ---------------------------------------------------------------------------
# Ethereum block times are well-behaved after The Merge (2022-09-15): ~12s per
# block. Pre-Merge it was ~13.2s. We only sample dates 2023+ so post-Merge
# math suffices for our tokens (fresh 2023-2024 pools).
MERGE_TIMESTAMP = 1663224162   # 2022-09-15 06:42:42 UTC
MERGE_BLOCK = 15537394
POST_MERGE_BLOCK_TIME = 12.0


def timestamp_to_block(ts: int) -> int:
    """Approximate Ethereum block number at a UTC timestamp.

    Accuracy: within a few hundred blocks (few thousand seconds). Fine for
    daily sampling where end-of-day precision is not critical.
    """
    if ts < MERGE_TIMESTAMP:
        return int((ts - 1438269973) / 13.2)
    return MERGE_BLOCK + int((ts - MERGE_TIMESTAMP) / POST_MERGE_BLOCK_TIME)


# ---------------------------------------------------------------------------
# Tick math: full Uniswap V3 tick-walking depth
# ---------------------------------------------------------------------------
def query_pool_at_timestamp(pool_id: str, timestamp: int,
                            quote_token_address: str) -> dict | None:
    """Fetch pool state, active ticks, and quote-token USD price at a block.

    The block is derived from timestamp via a rough approximation. We ask
    for pool state (tick, sqrtPrice, liquidity, TVL), active ticks with
    liquidityNet (for tick-walking), and the WETH/USDC price bundle for
    USD conversion.
    """
    block = timestamp_to_block(timestamp)
    query = """
    query PoolAtBlock($pool: String!, $block: Int!, $qt: String!, $ts: Int!) {
      pool(id: $pool, block: {number: $block}) {
        tick
        sqrtPrice
        liquidity
        feeTier
        totalValueLockedUSD
        token0 { id decimals }
        token1 { id decimals }
        ticks(
          where: {liquidityGross_gt: "0"}
          orderBy: tickIdx
          orderDirection: asc
          first: 1000
        ) {
          tickIdx
          liquidityNet
        }
      }
      bundle(id: "1", block: {number: $block}) {
        ethPriceUSD
      }
      tokenDayDatas(
        where: {token: $qt, date_lte: $ts}
        first: 1
        orderBy: date
        orderDirection: desc
      ) {
        date
        priceUSD
      }
    }
    """
    try:
        data = graphql(query, {
            "pool": pool_id.lower(),
            "block": block,
            "qt": quote_token_address.lower(),
            "ts": timestamp,
        })
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        # Compact the very verbose "bad indexers: {addr: ..., addr: ...}" message
        if "bad indexers" in msg:
            print(f"    tick-walking not available for block {block} "
                  "(archive indexers missing)")
        else:
            print(f"    query error at block {block}: {msg[:200]}")
        return None
    pool = data.get("pool")
    if pool is None:
        return None
    bundle = data.get("bundle") or {}
    token_days = data.get("tokenDayDatas") or []
    pool["_block_number"] = block
    pool["_eth_price_usd"] = float(bundle.get("ethPriceUSD") or 0) if bundle else 0.0
    pool["_quote_price_usd"] = (float(token_days[0]["priceUSD"])
                                if token_days else None)
    return pool


def compute_depth_walking(pool_state: dict, target_pct: float
                          ) -> dict | None:
    """Full tick-walking depth for a target percent downward price move.

    Walks through active ticks below current price, summing token amounts
    consumed until we reach the target sqrt price. Uses Uniswap V3 formulas:
        Δx = L × (1/√P_target - 1/√P_curr)     (token0 added)
        Δy = L × (√P_curr - √P_target)         (token1 received)
    At each tick crossing (going down): L -= liquidityNet(tick).

    Falls back to constant-L if the tick range is empty (fresh pool with no
    LP yet, or subgraph indexing gap).
    """
    try:
        sp_curr = int(pool_state["sqrtPrice"]) / (2 ** 96)
        L = int(pool_state["liquidity"])
        current_tick = int(pool_state["tick"])
        dec0 = int(pool_state["token0"]["decimals"])
        dec1 = int(pool_state["token1"]["decimals"])
    except (KeyError, ValueError, TypeError):
        return None
    if sp_curr <= 0 or L <= 0:
        return None

    ratio = 1.0 - target_pct
    if ratio <= 0:
        return None
    sp_target = sp_curr * math.sqrt(ratio)

    ticks = pool_state.get("ticks") or []
    lower = sorted(
        (t for t in ticks if int(t["tickIdx"]) <= current_tick),
        key=lambda t: -int(t["tickIdx"]),
    )

    if not lower:
        # Fallback: constant-L formula
        dx_raw = L * (1.0 / sp_target - 1.0 / sp_curr)
        dy_raw = L * (sp_curr - sp_target)
        return {
            "dx_token0_units": dx_raw / (10 ** dec0),
            "dy_token1_units": dy_raw / (10 ** dec1),
            "method": "constant_L",
            "ticks_crossed": 0,
        }

    total_dx = 0.0
    total_dy = 0.0
    sp = sp_curr
    ticks_crossed = 0

    for tick in lower:
        tick_idx = int(tick["tickIdx"])
        try:
            tick_sp = math.pow(1.0001, tick_idx / 2)
        except OverflowError:
            break

        if tick_sp <= sp_target:
            # Target lies inside the current L range; compute partial and stop
            dx = L * (1.0 / sp_target - 1.0 / sp)
            dy = L * (sp - sp_target)
            total_dx += dx
            total_dy += dy
            return {
                "dx_token0_units": total_dx / (10 ** dec0),
                "dy_token1_units": total_dy / (10 ** dec1),
                "method": "walking",
                "ticks_crossed": ticks_crossed,
            }

        # Cross this tick fully
        dx = L * (1.0 / tick_sp - 1.0 / sp)
        dy = L * (sp - tick_sp)
        total_dx += dx
        total_dy += dy
        # Going downward, liquidity update flips sign of liquidityNet
        L -= int(tick["liquidityNet"])
        sp = tick_sp
        ticks_crossed += 1
        if L <= 0:
            break

    # Fell off the end of active ticks without reaching target: report partial.
    # This means the pool has too little liquidity below current price to
    # absorb the target move without further LP support.
    return {
        "dx_token0_units": total_dx / (10 ** dec0),
        "dy_token1_units": total_dy / (10 ** dec1),
        "method": "walking_incomplete",
        "ticks_crossed": ticks_crossed,
    }


def sample_pool_day(pool_id: str, sample_date: datetime,
                    quote_token_address: str) -> dict | None:
    """Query pool state via poolDayDatas at end-of-day of sample_date.

    Also fetches the quote token's priceUSD at that day via tokenDayDatas,
    so the caller can convert depth-in-quote-token to depth-in-USD.
    """
    end_ts = int(sample_date.replace(
        hour=23, minute=59, second=59).timestamp())
    query = """
    query PoolDay($pool: String!, $ts: Int!, $qt: String!) {
      poolDayDatas(
        where: {pool: $pool, date_lte: $ts}
        first: 1
        orderBy: date
        orderDirection: desc
      ) {
        date
        tick
        sqrtPrice
        liquidity
        volumeUSD
        tvlUSD
        feesUSD
        open
        close
      }
      tokenDayDatas(
        where: {token: $qt, date_lte: $ts}
        first: 1
        orderBy: date
        orderDirection: desc
      ) {
        date
        priceUSD
      }
    }
    """
    data = graphql(query, {"pool": pool_id.lower(), "ts": end_ts,
                           "qt": quote_token_address.lower()})
    days = data.get("poolDayDatas") or []
    if not days:
        return None
    result = dict(days[0])
    quote_days = data.get("tokenDayDatas") or []
    if quote_days:
        result["quotePriceUSD"] = float(quote_days[0]["priceUSD"])
    else:
        result["quotePriceUSD"] = None
    return result


# ---------------------------------------------------------------------------
# Depth math (constant-liquidity approximation)
# ---------------------------------------------------------------------------
def compute_depth(sqrt_price_x96: str, liquidity: str,
                  decimals0: int, decimals1: int,
                  target_pct: float) -> dict | None:
    """Approximate depth at a target % downward price move using active L.

    Returns dict with token0/token1 amounts consumed to move price by
    target_pct downward, or None if inputs are invalid.
    """
    try:
        sp_curr = int(sqrt_price_x96) / (2 ** 96)
        L = int(liquidity)
    except (ValueError, TypeError):
        return None
    if sp_curr <= 0 or L <= 0:
        return None

    ratio = 1.0 - target_pct
    if ratio <= 0:
        return None
    sp_new = sp_curr * math.sqrt(ratio)

    # Uniswap V3 formulas for a swap that moves price down (adds token0, removes token1)
    dx_raw = L * (1.0 / sp_new - 1.0 / sp_curr)
    dy_raw = L * (sp_curr - sp_new)

    return {
        "dx_token0_units": dx_raw / (10 ** decimals0),
        "dy_token1_units": dy_raw / (10 ** decimals1),
    }


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------
def sample_corpus(pools: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, pool in pools.iterrows():
        tid = pool["token_id"]
        pool_id = pool["pool_id"]
        if pd.isna(pool_id) or not pool_id:
            print(f"[{tid}] skip: no pool discovered")
            continue
        created = datetime.fromisoformat(pool["created_at"])
        dec0 = int(pool["token0_decimals"])
        dec1 = int(pool["token1_decimals"])
        print(f"\n[{tid}] {pool['name']} pool {pool_id}")

        quote_token = pool["token1_id"]

        for offset in SAMPLE_DAYS:
            sample_date = created + timedelta(days=offset - 1)
            end_of_day_ts = int(sample_date.replace(
                hour=23, minute=59, second=59).timestamp())
            print(f"  day {offset:3d} -> {sample_date.date()}", flush=True)

            # Try tick-walking first (works only for recent blocks with archive
            # indexers). If it fails, fall back to poolDayDatas + constant-L
            # which works reliably for historical dates.
            state = query_pool_at_timestamp(pool_id, end_of_day_ts,
                                            quote_token)
            used_fallback = False
            if state is None:
                # Fallback: daily snapshot via poolDayDatas
                day_data = sample_pool_day(pool_id, sample_date, quote_token)
                if day_data is not None:
                    # Reshape into pool-state-compatible dict for compute_depth_walking
                    state = {
                        "tick": day_data["tick"],
                        "sqrtPrice": day_data["sqrtPrice"],
                        "liquidity": day_data["liquidity"],
                        "totalValueLockedUSD": day_data.get("tvlUSD"),
                        "token0": {"decimals": dec0},
                        "token1": {"decimals": dec1},
                        "ticks": [],  # forces constant_L fallback in depth calc
                        "_block_number": None,
                        "_quote_price_usd": day_data.get("quotePriceUSD"),
                    }
                    used_fallback = True
                    print(f"    → fallback to poolDayDatas: OK")
                else:
                    print(f"    → fallback to poolDayDatas: also failed")

            if state is None:
                rows.append({
                    "token_id": tid, "pool_id": pool_id,
                    "day_offset": offset,
                    "sample_date": sample_date.strftime("%Y-%m-%d"),
                    "block_number": None, "tick": None, "tvl_usd": None,
                    "quote_price_usd": None,
                    "depth_1pct_token0": None, "depth_1pct_token1": None,
                    "depth_1pct_usd": None, "depth_1pct_pct_of_tvl": None,
                    "depth_1pct_method": None, "depth_1pct_ticks_crossed": None,
                    "depth_5pct_token0": None, "depth_5pct_token1": None,
                    "depth_5pct_usd": None, "depth_5pct_pct_of_tvl": None,
                    "depth_5pct_method": None, "depth_5pct_ticks_crossed": None,
                    "status": "no_data",
                })
                time.sleep(SLEEP_BETWEEN_QUERIES)
                continue

            depth_1 = compute_depth_walking(state, 0.01)
            depth_5 = compute_depth_walking(state, 0.05)

            tvl_usd = (float(state["totalValueLockedUSD"])
                       if state.get("totalValueLockedUSD") else None)
            quote_price = state.get("_quote_price_usd")

            def _to_usd(d, price):
                if d is None or price is None:
                    return None
                return d["dy_token1_units"] * price

            def _pct_of_tvl(usd, tvl):
                if usd is None or tvl is None or tvl <= 0:
                    return None
                return usd / tvl * 100

            d1_usd = _to_usd(depth_1, quote_price)
            d5_usd = _to_usd(depth_5, quote_price)

            rows.append({
                "token_id": tid, "pool_id": pool_id,
                "day_offset": offset,
                "sample_date": sample_date.strftime("%Y-%m-%d"),
                "block_number": state.get("_block_number"),
                "tick": int(state["tick"]) if state.get("tick") is not None else None,
                "tvl_usd": tvl_usd,
                "quote_price_usd": quote_price,
                "used_fallback": used_fallback,
                "depth_1pct_token0": depth_1["dx_token0_units"] if depth_1 else None,
                "depth_1pct_token1": depth_1["dy_token1_units"] if depth_1 else None,
                "depth_1pct_usd": d1_usd,
                "depth_1pct_pct_of_tvl": _pct_of_tvl(d1_usd, tvl_usd),
                "depth_1pct_method": depth_1["method"] if depth_1 else None,
                "depth_1pct_ticks_crossed": depth_1["ticks_crossed"] if depth_1 else None,
                "depth_5pct_token0": depth_5["dx_token0_units"] if depth_5 else None,
                "depth_5pct_token1": depth_5["dy_token1_units"] if depth_5 else None,
                "depth_5pct_usd": d5_usd,
                "depth_5pct_pct_of_tvl": _pct_of_tvl(d5_usd, tvl_usd),
                "depth_5pct_method": depth_5["method"] if depth_5 else None,
                "depth_5pct_ticks_crossed": depth_5["ticks_crossed"] if depth_5 else None,
                "status": "ok",
            })
            time.sleep(SLEEP_BETWEEN_QUERIES)

    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true",
                    help="Print the token catalogue and exit")
    ap.add_argument("--discover", action="store_true",
                    help="Only find pool addresses for the catalogue")
    ap.add_argument("--use-cached-pools", action="store_true",
                    help="Skip discovery, load pools from dex_pools_discovered.csv")
    args = ap.parse_args()

    corpus = pd.read_csv(DEX_CSV, dtype=str)
    if args.list:
        print(corpus.to_string(index=False))
        return 0

    if args.use_cached_pools and POOLS_CSV.exists():
        print(f"Loading cached pools from {POOLS_CSV}")
        pools = pd.read_csv(POOLS_CSV)
    else:
        print(f"Discovering Uniswap V3 pools for {len(corpus)} tokens")
        pools = discover_all_pools()
        if args.discover:
            return 0

    if pools.empty:
        print("No pools available to sample")
        return 1

    print(f"\nSampling {len(pools)} pools at {len(SAMPLE_DAYS)} days each")
    df = sample_corpus(pools)
    df.to_csv(OUT_CSV, index=False)
    print(f"\nWrote {len(df)} rows -> {OUT_CSV}")

    ok = df[df.status == "ok"]
    print(f"\n=== Status coverage ===")
    print(df.groupby(["token_id", "status"]).size().unstack(fill_value=0))

    if not ok.empty:
        print(f"\n=== Fallback path usage (indexer archive miss vs tick-walking) ===")
        print(f"Used poolDayDatas fallback: {ok['used_fallback'].sum()} of {len(ok)} samples")
        print(f"\n=== Depth-1% method (walking = real ticks, constant_L = fallback approx) ===")
        print(ok["depth_1pct_method"].value_counts().to_string())
        print(f"\n=== Depth-1% ticks crossed (median по методу walking) ===")
        walking = ok[ok["depth_1pct_method"] == "walking"]
        if not walking.empty:
            print(walking["depth_1pct_ticks_crossed"].describe().round(1).to_string())

        print(f"\n=== Depth-1% в USD (median) ===")
        by_day_usd = ok.groupby("day_offset")["depth_1pct_usd"].agg(
            ["count", "median", "min", "max"])
        print(by_day_usd.round(0))

        print(f"\n=== Depth-1% как % от TVL пула (median) ===")
        by_day_pct = ok.groupby("day_offset")["depth_1pct_pct_of_tvl"].agg(
            ["count", "median", "min", "max"])
        print(by_day_pct.round(4))

        print(f"\n=== TVL пулов в USD (M) по дням ===")
        tvl = ok.pivot_table(index="day_offset", columns="token_id",
                             values="tvl_usd", aggfunc="first") / 1e6
        print(tvl.round(2).to_string())

    return 0


if __name__ == "__main__":
    sys.exit(main())
