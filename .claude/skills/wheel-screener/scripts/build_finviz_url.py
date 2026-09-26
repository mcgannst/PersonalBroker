#!/usr/bin/env python3
"""Build FinViz screener URLs that pre-filter for the eight-test wheel framework.

FinViz only offers preset price buckets, so the script picks the tightest
preset that fully contains the requested price range and reports the exact
bounds to post-filter on.

Examples:
    python build_finviz_url.py --max-price 40
    python build_finviz_url.py --max-price 75 --min-price 10 --tier wide
    python build_finviz_url.py --cash-usd 5000          # max price = cash / 100
    python build_finviz_url.py --max-price 40 --json
"""
import argparse
import json
import math
import sys

BASE = "https://finviz.com/screener.ashx"

# View numbers: overview, valuation, financial, technical.
VIEWS = {"overview": 111, "valuation": 121, "financial": 161, "technical": 171}

# Always on: optionable US-listed common stocks (no ETFs/funds).
BASE_FILTERS = ["sh_opt_option", "ind_stocksonly", "geo_usa"]

# Tier filters map to wheel tests 2 (profitability), 3 (balance sheet),
# 4 (size) and 5 (trend). Average volume is only a pre-screen; Test 6 is
# still decided by the real option chain.
TIERS = {
    # Every hard test at PASS level, plus a cooler RSI and golden-cross trend.
    "strict": ["cap_largeover", "fa_pe_profitable", "fa_debteq_u1",
               "ta_sma50_pa", "ta_sma200_sb50", "ta_rsi_nob60",
               "sh_avgvol_o1000"],
    # Framework PASS thresholds that FinViz can express directly.
    "standard": ["cap_largeover", "fa_pe_profitable", "fa_debteq_u1",
                 "ta_sma50_pa", "sh_avgvol_o500"],
    # Lets in CAUTION-tier size ($2-10B) and drops the Debt/Eq filter so
    # utilities/telecom/pipelines (allowed up to 2) are not excluded.
    "wide": ["cap_midover", "fa_pe_profitable", "ta_sma50_pa",
             "sh_avgvol_o500"],
}

# FinViz price presets as (code, low, high).
_UNDER = [1, 2, 3, 4, 5, 7, 10, 15, 20, 30, 40, 50]
_OVER = [1, 2, 3, 4, 5, 7, 10, 15, 20, 30, 40, 50, 60, 70, 80, 90, 100]
_RANGES = [(1, 5), (1, 10), (1, 20), (5, 10), (5, 20), (5, 50), (10, 20),
           (10, 50), (20, 50), (50, 100)]
PRICE_PRESETS = (
    [(f"sh_price_u{n}", 0.0, float(n)) for n in _UNDER]
    + [(f"sh_price_o{n}", float(n), math.inf) for n in _OVER]
    + [(f"sh_price_{a}to{b}", float(a), float(b)) for a, b in _RANGES]
)


def pick_price_filter(min_price, max_price):
    """Return the narrowest preset containing [min_price, max_price], or None."""
    fits = [p for p in PRICE_PRESETS if p[1] <= min_price and p[2] >= max_price]
    if not fits:
        return None
    # Narrowest window wins; among open-ended "over" presets, the highest floor.
    return min(fits, key=lambda p: (p[2] - p[1], -p[1]))


def build(max_price, min_price, tier, sort):
    price = pick_price_filter(min_price, max_price)
    filters = BASE_FILTERS + TIERS[tier] + ([price[0]] if price else [])
    f = ",".join(filters)
    urls = {name: f"{BASE}?v={v}&f={f}&o={sort}" for name, v in VIEWS.items()}
    return {
        "tier": tier,
        "min_price": min_price,
        "max_price": max_price,
        "price_filter": price[0] if price else None,
        "post_filter_price": price is None or price[1] < min_price or price[2] > max_price,
        "max_collateral_usd": round(max_price * 100, 2),
        "filters": filters,
        "urls": urls,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    cap = ap.add_mutually_exclusive_group(required=True)
    cap.add_argument("--max-price", type=float, help="Highest stock price (USD) to include")
    cap.add_argument("--cash-usd", type=float,
                     help="Cash for one contract; max price = cash / 100")
    ap.add_argument("--min-price", type=float, default=5.0,
                    help="Lowest stock price (default 5, avoids penny stocks)")
    ap.add_argument("--tier", choices=TIERS, default="standard")
    ap.add_argument("--sort", default="-marketcap",
                    help="FinViz sort key (default -marketcap; never sort by yield)")
    ap.add_argument("--json", action="store_true", help="Print JSON instead of text")
    args = ap.parse_args(argv)

    max_price = args.max_price if args.max_price is not None else args.cash_usd / 100
    if max_price <= args.min_price:
        ap.error(f"max price {max_price:.2f} must be above min price {args.min_price:.2f}")

    out = build(max_price, args.min_price, args.tier, args.sort)
    if args.json:
        json.dump(out, sys.stdout, indent=2)
        print()
        return
    print(f"Tier: {out['tier']}   Price window: ${args.min_price:.2f}-${max_price:.2f}"
          f"   Max collateral/contract: ${out['max_collateral_usd']:,.2f} USD")
    print(f"Price preset: {out['price_filter'] or 'none (no preset fits)'}"
          + ("   -> post-filter exact price window" if out["post_filter_price"] else ""))
    print(f"Filters: {','.join(out['filters'])}")
    for name, url in out["urls"].items():
        print(f"{name:>10}: {url}")


if __name__ == "__main__":
    main()
