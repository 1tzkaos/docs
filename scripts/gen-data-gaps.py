#!/usr/bin/env python3
"""Generate `snippets/data-gaps.mdx` from the live index.

The coverage page's gap disclosure is MEASURED, never hand-maintained: every
number in the generated snippet comes from one of the queries below, run
against ClickHouse at generation time. Re-run this after a backfill and healed
windows drop out of the table on their own.

Usage:
    CLICKHOUSE_URL='http://user:pass@host:8123/' python3 scripts/gen-data-gaps.py
    python3 scripts/gen-data-gaps.py --json out.json    # also dump raw measurements

`CLICKHOUSE_URL` needs SELECT on `solana_swaps.*` and `system.tables` only —
`dexploit_ro_user` is sufficient. It does NOT need `system.parts`.

The generated snippet is committed. `scripts/build-llms-full.sh` inlines it into
llms-full.txt through the normal snippet mechanism, and only re-runs this script
when `REGEN_DATA_GAPS=1` is set, so a docs build never depends on database
reachability.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.request
from datetime import datetime, timezone

# A venue that goes quiet for a single hour is usually a thin venue with no
# SOL-quoted trades that hour, not an ingestion failure. Two or more
# consecutive hours of silence on a venue whose median hour carries thousands
# of rows is an outage. Only runs at or above this length are published.
MIN_GAP_HOURS = 2

# An hour carrying under this fraction of the venue's median hour is reported
# as "degraded" rather than absent — the census definition from #690.
LOW_FRACTION = 0.1

# The eight live candle tables, newest-resolution first. Spelled out rather
# than matched with `ohlcv_%` on purpose: the database also holds stale
# `ohlcv_*_dup379_backup` tables.
OHLCV_TABLES = [
    "ohlcv_1s",
    "ohlcv_30s",
    "ohlcv_1m",
    "ohlcv_5m",
    "ohlcv_15m",
    "ohlcv_1h",
    "ohlcv_4h",
    "ohlcv_1d",
]

# Shyft's getBlock retention, measured 2026-09-09 (#710): OK at 90 h, refused
# at 96 h. A gap younger than this is recoverable from Shyft at no credit cost;
# an older one needs a paid archival RPC.
SHYFT_RETENTION_HOURS = 90

# Per-venue hourly census. `absent` counts hours between a venue's first and
# last indexed hour that carry no rows at all; hours before a venue was added
# are never counted against it.
Q_CENSUS = """
WITH hourly AS (
  SELECT dex, toStartOfHour(timestamp) AS h, count() AS n
  FROM solana_swaps.pump_swaps
  GROUP BY dex, h
),
med AS (SELECT dex, median(n) AS m FROM hourly GROUP BY dex)
SELECT h.dex AS venue,
       toString(min(h.h)) AS first_hour,
       toString(max(h.h)) AS last_hour,
       dateDiff('hour', min(h.h), max(h.h)) + 1 AS expected_hours,
       count() AS observed_hours,
       (dateDiff('hour', min(h.h), max(h.h)) + 1) - count() AS hours_absent,
       countIf(h.n < {low} * m.m) AS hours_degraded,
       round(any(m.m)) AS median_rows_per_hour
FROM hourly h INNER JOIN med m ON h.dex = m.dex
GROUP BY h.dex
ORDER BY hours_absent DESC, venue
FORMAT JSON
"""

# Contiguous absent windows, derived from the gaps between consecutive indexed
# hours. Venues that share an ingestor fail together, so windows are grouped by
# their exact (start, end) and carry the full venue list.
Q_GAPS = """
WITH hourly AS (
  SELECT dex, toStartOfHour(timestamp) AS h
  FROM solana_swaps.pump_swaps
  GROUP BY dex, h
),
arr AS (SELECT dex, arraySort(groupArray(h)) AS hs FROM hourly GROUP BY dex),
gaps AS (
  SELECT dex, arrayJoin(arrayFilter(x -> tupleElement(x, 3) > 0,
    arrayMap((a, b) -> (a, b, dateDiff('hour', a, b) - 1),
             arraySlice(hs, 1, length(hs) - 1), arraySlice(hs, 2)))) AS g
  FROM arr
)
SELECT toString(tupleElement(g, 1) + INTERVAL 1 HOUR) AS gap_start,
       toString(tupleElement(g, 2)) AS resumed,
       tupleElement(g, 3) AS hours,
       arraySort(groupArray(dex)) AS venues,
       max(dateDiff('hour', tupleElement(g, 2), now())) AS age_hours
FROM gaps
WHERE hours >= {min_gap}
GROUP BY gap_start, resumed, hours
ORDER BY gap_start
FORMAT JSON
"""

# Retention ladder + the oldest bar actually held, per timeframe. The oldest
# bar is read from the table itself, NOT from `system.parts.min_time` — a live
# `ohlcv_1s` part reports a 1970-01-01 `min_time` while holding no row older
# than 2026-06-11 (#719).
Q_CANDLE_BOUNDS = """
SELECT '{table}' AS tbl,
       toString(min(timestamp)) AS oldest,
       toString(max(timestamp)) AS newest,
       count() AS rows
FROM solana_swaps.{table}
FORMAT JSON
"""

Q_TTL = """
SELECT name,
       toInt32OrNull(extract(engine_full, 'toIntervalDay\\\\((\\\\d+)\\\\)')) AS ttl_days
FROM system.tables
WHERE database = 'solana_swaps' AND name IN ({tables})
FORMAT JSON
"""

VENUE_LABELS = {
    "pumpfun": "Pump.fun",
    "pumpswap": "PumpSwap",
    "raydium_amm": "Raydium AMM v4",
    "raydium_clmm": "Raydium CLMM",
    "raydium_cpmm": "Raydium CPMM",
    "orca": "Orca Whirlpools",
    "meteora_damm_v2": "Meteora DAMM v2",
    "meteora_dbc": "Meteora DBC",
    "meteora_dlmm": "Meteora DLMM",
    "meteora_pools": "Meteora Pools",
}

TIMEFRAME_LABELS = {
    "ohlcv_1s": "1s",
    "ohlcv_30s": "30s",
    "ohlcv_1m": "1m",
    "ohlcv_5m": "5m",
    "ohlcv_15m": "15m",
    "ohlcv_1h": "1h",
    "ohlcv_4h": "4h",
    "ohlcv_1d": "1d",
}


def query(url: str, sql: str) -> list[dict]:
    """Run one SELECT and return its rows. Any non-SELECT is refused here."""
    if not sql.lstrip().upper().startswith(("SELECT", "WITH")):
        raise ValueError("gen-data-gaps issues read-only queries only")
    req = urllib.request.Request(url, data=sql.encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read().decode())["data"]
    except urllib.error.HTTPError as e:
        # ClickHouse puts the real diagnosis in the body; a bare "HTTP 500" is
        # useless when a query has a dozen clauses.
        raise SystemExit(f"clickhouse rejected the query:\n{e.read().decode()[:800]}") from e


def day(ts: str) -> str:
    """`2026-05-18 10:00:00` -> `2026-05-18`."""
    return ts.split(" ")[0]


def hour(ts: str) -> str:
    """`2026-05-18 10:00:00` -> `2026-05-18 10:00Z`."""
    d, t = ts.split(" ")
    return f"{d} {t[:5]}Z"


def duration(hours: int) -> str:
    if hours < 24:
        return f"{hours} h"
    days, rem = divmod(hours, 24)
    return f"{days} d {rem} h" if rem else f"{days} d"


def venue_list(venues: list[str]) -> str:
    """Collapse a venue set to something a reader can scan."""
    if len(venues) == len(VENUE_LABELS):
        return "**every venue**"
    families = {
        "raydium": {"raydium_amm", "raydium_clmm", "raydium_cpmm"},
        "meteora": {"meteora_damm_v2", "meteora_dbc", "meteora_dlmm", "meteora_pools"},
    }
    got = set(venues)
    for name, members in families.items():
        if got == members:
            return f"all {name.capitalize()} venues"
    return ", ".join(f"`{v}`" for v in venues)


def backfill_status(age_hours: int) -> str:
    """Whether a window can still be recovered, from its age alone.

    Phrased for a reader deciding whether to wait for the hole to fill, not
    for whoever runs the backfill.
    """
    if age_hours <= SHYFT_RETENTION_HOURS:
        return "Backfill pending"
    return "Past upstream retention"


def render(census, gaps, ladder, generated_at: str) -> str:
    swap_start = min(day(r["first_hour"]) for r in census)
    oldest_candle = min(r["oldest"] for r in ladder if r["oldest"])

    out: list[str] = []
    w = out.append

    w("{/* GENERATED by scripts/gen-data-gaps.py — do not edit by hand. */}")
    w("")
    w("## Where the data starts")
    w("")
    w("Three different dates get called \"the start of the index\", and they measure")
    w("different things. These are the current values, read from the tables that serve")
    w("your queries:")
    w("")
    w("| Surface | Data begins | What it means |")
    w("| --- | --- | --- |")
    w(
        f"| Raw swaps (`/swaps*`) | **{swap_start}** | The oldest indexed swap. "
        "This table has no expiry — nothing is aged out of it. |"
    )
    w(
        f"| Candles (`/api/v1/candles`) | **{day(oldest_candle)}** | The oldest bar on any "
        "timeframe. Shorter timeframes start later — see the retention ladder below. |"
    )
    w(
        "| `creator` / deployer fields | **2026-05-26** | When Phase 0 mint capture went "
        "live. Mints that first traded before this date usually have no `creator`. |"
    )
    w("")
    w(
        "`GET /api/v1/stats` reports the candle figure as `oldest_candle`, computed from "
        "the same tables — it is the one to trust programmatically."
    )
    w("")
    w("### Candle retention ladder")
    w("")
    w(
        "Candle timeframes expire on different schedules, so \"how far back can I go\" "
        "depends on which timeframe you ask for. A finer timeframe is not available for "
        "the whole history:"
    )
    w("")
    w("| Timeframe | Retention | Oldest bar held |")
    w("| --- | --- | --- |")
    for row in ladder:
        tf = TIMEFRAME_LABELS[row["tbl"]]
        ttl = row.get("ttl_days")
        retention = f"{ttl} days" if ttl else "No expiry"
        w(f"| `{tf}` | {retention} | {hour(row['oldest'])} |")
    w("")
    w(
        "Daily bars are kept forever, so `1d` is the timeframe to use for a full-history "
        "series."
    )
    w("")
    w("## Known data gaps")
    w("")
    w(
        f"Every figure on this page was measured against the live index at "
        f"**{generated_at}**. It is generated, not maintained by hand, so it cannot "
        "drift from what the API actually returns."
    )
    w("")

    if not gaps:
        w(
            "No multi-hour ingestion gap is currently outstanding on any venue. "
            "This section is generated, so a gap would appear here automatically."
        )
        w("")
    else:
        w(
            "An ingestion outage leaves a venue with **no rows at all** for the affected "
            f"hours — not partial data. Every window of {MIN_GAP_HOURS} hours or more that "
            "is still outstanding is listed here. Windows drop off this table once they "
            "are backfilled."
        )
        w("")
        w("| Window (UTC) | Duration | Affected | Status |")
        w("| --- | --- | --- | --- |")
        for g in gaps:
            start, resumed = hour(g["gap_start"]), hour(g["resumed"])
            w(
                f"| {start} → {resumed} | {duration(int(g['hours']))} | "
                f"{venue_list(g['venues'])} | {backfill_status(int(g['age_hours']))} |"
            )
        w("")
        w(
            "**Backfill pending** means the blocks are still inside our upstream "
            "provider's retention window and the hole is queued to be filled. "
            "**Past upstream retention** means the source blocks have aged out; those "
            "windows may never be filled, so treat them as permanent holes when you "
            "design around them."
        )
        w("")

    w("### Per-venue totals")
    w("")
    w(
        "Across each venue's own indexed history — hours before a venue was added are not "
        "counted against it:"
    )
    w("")
    w("| Venue | Indexed since | Hours missing | Degraded hours | Median rows/hour |")
    w("| --- | --- | --- | --- | --- |")
    for r in census:
        label = VENUE_LABELS.get(r["venue"], r["venue"])
        absent = int(r["hours_absent"])
        expected = int(r["expected_hours"])
        pct = f" ({absent / expected * 100:.1f}%)" if absent else ""
        w(
            f"| {label} (`{r['venue']}`) | {day(r['first_hour'])} | {absent:,}{pct} | "
            f"{int(r['hours_degraded']):,} | {int(r['median_rows_per_hour']):,} |"
        )
    w("")
    w(
        f"A \"degraded\" hour carries under {int(LOW_FRACTION * 100)}% of that venue's median "
        "hour. On a thin venue that is often just a quiet hour rather than a fault, which is "
        f"why only runs of {MIN_GAP_HOURS}+ fully-empty hours are listed as gaps above."
    )
    w("")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--output",
        default="snippets/data-gaps.mdx",
        help="snippet to write (default: snippets/data-gaps.mdx)",
    )
    ap.add_argument("--json", help="also write the raw measurements here")
    args = ap.parse_args()

    url = os.environ.get("CLICKHOUSE_URL", "").strip()
    if not url:
        print(
            "CLICKHOUSE_URL is unset. This script measures the live index; it has no "
            "offline mode.\nThe committed snippet stays valid until you re-run it.",
            file=sys.stderr,
        )
        return 2

    root = pathlib.Path(__file__).resolve().parent.parent
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")

    census = query(url, Q_CENSUS.format(low=LOW_FRACTION))
    gaps = query(url, Q_GAPS.format(min_gap=MIN_GAP_HOURS))

    ttl_rows = query(
        url, Q_TTL.format(tables=",".join(f"'{t}'" for t in OHLCV_TABLES))
    )
    ttls = {r["name"]: r["ttl_days"] for r in ttl_rows}
    ladder = []
    for table in OHLCV_TABLES:
        row = query(url, Q_CANDLE_BOUNDS.format(table=table))[0]
        row["ttl_days"] = ttls.get(table)
        ladder.append(row)

    snippet = render(census, gaps, ladder, generated_at)
    out_path = root / args.output
    out_path.write_text(snippet)
    print(f"wrote {out_path.relative_to(root)} ({len(gaps)} gap windows)", file=sys.stderr)

    if args.json:
        pathlib.Path(args.json).write_text(
            json.dumps(
                {
                    "generated_at": generated_at,
                    "census": census,
                    "gaps": gaps,
                    "ladder": ladder,
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
