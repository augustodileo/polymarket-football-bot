#!/usr/bin/env python3
"""
analyze.py — Analyze bot trading performance.

Usage:
    uv run analyze.py              # analyze all paper trades
    uv run analyze.py --today      # analyze today's paper trades only
    uv run analyze.py --date 2026-03-21          # specific day
    uv run analyze.py --from 2026-03-20 --to 2026-03-22  # date range
    uv run analyze.py --live       # analyze live trades
    uv run analyze.py --all        # analyze both paper and live
    uv run analyze.py --wipe-paper # delete all paper data and start fresh
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"


def load_trades(mode: str, date_from: str = "", date_to: str = "") -> list[dict]:
    """Load trades from daily files (trades_YYYY-MM-DD.jsonl).

    Also reads legacy trades.jsonl if it exists.
    Optionally filter by date range.
    """
    mode_dir = DATA_DIR / mode
    if not mode_dir.exists():
        return []

    trades = []

    # Read all daily trade files + legacy file
    for f in sorted(mode_dir.glob("trades*.jsonl")):
        # Extract date from filename if it's a daily file
        fname = f.stem  # e.g. "trades_2026-03-21" or "trades"
        file_date = fname.replace("trades_", "").replace("trades", "")

        # Filter by date range if specified
        if file_date and date_from and file_date < date_from:
            continue
        if file_date and date_to and file_date > date_to:
            continue

        with open(f) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    trades.append(json.loads(line))

    return trades


def print_summary(trades: list[dict], label: str):
    if not trades:
        print(f"\n  [{label}] No trades yet.\n")
        return

    wins = [t for t in trades if t.get("outcome") == "WIN"]
    losses = [t for t in trades if t.get("outcome") == "LOSS"]
    total_pnl = sum(t.get("pnl", 0) for t in trades)
    total_staked = sum(t.get("stake", 0) for t in trades)
    avg_edge = sum(t.get("edge_pct", 0) for t in trades) / len(trades) if trades else 0
    avg_stake = total_staked / len(trades) if trades else 0
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    roi = (total_pnl / total_staked * 100) if total_staked > 0 else 0

    # Date range
    dates = [t.get("timestamp", "")[:10] for t in trades if t.get("timestamp")]
    first = min(dates) if dates else "?"
    last = max(dates) if dates else "?"

    print()
    print("=" * 65)
    print(f"  [{label.upper()}] PERFORMANCE REPORT")
    print(f"  Period: {first} to {last}")
    print("=" * 65)
    print()
    print(f"  Trades:     {len(trades)}")
    print(f"  Wins:       {len(wins)}")
    print(f"  Losses:     {len(losses)}")
    print(f"  Win Rate:   {win_rate:.1f}%")
    print()
    print(f"  Total P&L:  ${total_pnl:+,.2f}")
    print(f"  Total Staked: ${total_staked:,.2f}")
    print(f"  ROI:        {roi:+.1f}%")
    print(f"  Avg Stake:  ${avg_stake:,.2f}")
    print(f"  Avg Edge:   {avg_edge:.1f}%")
    print()

    # P&L by league
    by_league: dict[str, list] = {}
    for t in trades:
        lg = t.get("league") or "Unknown"
        by_league.setdefault(lg, []).append(t)

    if len(by_league) > 1 or (len(by_league) == 1 and list(by_league.keys())[0] != "Unknown"):
        print("  BY LEAGUE:")
        print(f"  {'League':<25s} {'Trades':>6s} {'W':>4s} {'L':>4s} {'Win%':>6s} {'P&L':>10s} {'ROI':>7s}")
        print("  " + "-" * 63)
        for lg, lt in sorted(by_league.items(), key=lambda x: -sum(t["pnl"] for t in x[1])):
            lw = sum(1 for t in lt if t.get("outcome") == "WIN")
            ll = sum(1 for t in lt if t.get("outcome") == "LOSS")
            lpnl = sum(t.get("pnl", 0) for t in lt)
            lstk = sum(t.get("stake", 0) for t in lt)
            lwr = lw / len(lt) * 100 if lt else 0
            lroi = lpnl / lstk * 100 if lstk > 0 else 0
            print(f"  {lg:<25s} {len(lt):>6d} {lw:>4d} {ll:>4d} {lwr:>5.0f}% ${lpnl:>+9.2f} {lroi:>+6.1f}%")
        print()

    # P&L by trade type
    by_type: dict[str, list] = {}
    for t in trades:
        tt = t.get("trade_type") or "STANDARD"
        by_type.setdefault(tt, []).append(t)

    if len(by_type) > 1:
        print("  BY TRADE TYPE:")
        print(f"  {'Type':<25s} {'Trades':>6s} {'W':>4s} {'L':>4s} {'Win%':>6s} {'P&L':>10s}")
        print("  " + "-" * 56)
        for tt, lt in sorted(by_type.items(), key=lambda x: -sum(t["pnl"] for t in x[1])):
            tw = sum(1 for t in lt if t.get("outcome") == "WIN")
            tl = sum(1 for t in lt if t.get("outcome") == "LOSS")
            tpnl = sum(t.get("pnl", 0) for t in lt)
            twr = tw / len(lt) * 100 if lt else 0
            print(f"  {tt:<25s} {len(lt):>6d} {tw:>4d} {tl:>4d} {twr:>5.0f}% ${tpnl:>+9.2f}")
        print()

    # P&L by edge range
    edge_buckets = {"0-2%": [], "2-5%": [], "5-10%": [], "10%+": []}
    for t in trades:
        e = abs(t.get("edge_pct", 0))
        if e < 2:
            edge_buckets["0-2%"].append(t)
        elif e < 5:
            edge_buckets["2-5%"].append(t)
        elif e < 10:
            edge_buckets["5-10%"].append(t)
        else:
            edge_buckets["10%+"].append(t)

    print("  BY EDGE RANGE:")
    print(f"  {'Edge':<12s} {'Trades':>6s} {'W':>4s} {'L':>4s} {'Win%':>6s} {'P&L':>10s}")
    print("  " + "-" * 43)
    for bucket, lt in edge_buckets.items():
        if not lt:
            continue
        bw = sum(1 for t in lt if t.get("outcome") == "WIN")
        bl = sum(1 for t in lt if t.get("outcome") == "LOSS")
        bpnl = sum(t.get("pnl", 0) for t in lt)
        bwr = bw / len(lt) * 100 if lt else 0
        print(f"  {bucket:<12s} {len(lt):>6d} {bw:>4d} {bl:>4d} {bwr:>5.0f}% ${bpnl:>+9.2f}")
    print()

    # Recent trades
    recent = trades[-10:]
    print("  RECENT TRADES:")
    print(f"  {'Date':<12s} {'Match':<30s} {'Side':<4s} {'Edge':>5s} {'Stake':>8s} {'Result':>7s} {'P&L':>9s}")
    print("  " + "-" * 76)
    for t in recent:
        dt = (t.get("timestamp") or "")[:10]
        ev = (t.get("event") or "")[:29]
        side = t.get("side", "?")
        edge = f"{t.get('edge_pct', 0):.1f}%"
        stake = f"${t.get('stake', 0):.0f}"
        result = t.get("outcome", "?")
        pnl = f"${t.get('pnl', 0):+.2f}"
        print(f"  {dt:<12s} {ev:<30s} {side:<4s} {edge:>5s} {stake:>8s} {result:>7s} {pnl:>9s}")
    print()

    # Cumulative P&L curve (text sparkline)
    if len(trades) >= 3:
        cum_pnl = []
        running = 0.0
        for t in trades:
            running += t.get("pnl", 0)
            cum_pnl.append(running)
        min_pnl = min(cum_pnl)
        max_pnl = max(cum_pnl)
        rng = max_pnl - min_pnl if max_pnl != min_pnl else 1
        bar_width = 50
        print("  P&L CURVE:")
        for i, cp in enumerate(cum_pnl):
            pos = int((cp - min_pnl) / rng * bar_width)
            bar = " " * pos + "|"
            marker = "W" if trades[i].get("outcome") == "WIN" else "L"
            print(f"  {marker} {bar} ${cp:+.0f}")
        print()

    print("=" * 65)
    print()


def wipe_paper():
    paper_dir = DATA_DIR / "paper"
    if not paper_dir.exists():
        print("No paper data to wipe.")
        return
    count = 0
    for f in paper_dir.iterdir():
        f.unlink()
        count += 1
    print(f"Wiped {count} files from {paper_dir}")


def list_days(mode: str):
    """Show available trade files."""
    mode_dir = DATA_DIR / mode
    if not mode_dir.exists():
        print(f"  No {mode} data directory.")
        return
    files = sorted(mode_dir.glob("trades*.jsonl"))
    if not files:
        print(f"  No {mode} trade files.")
        return
    print(f"\n  [{mode.upper()}] Trade files:")
    for f in files:
        count = sum(1 for line in open(f) if line.strip())
        trades = [json.loads(line) for line in open(f) if line.strip()]
        pnl = sum(t.get("pnl", 0) for t in trades)
        wins = sum(1 for t in trades if t.get("outcome") == "WIN")
        losses = sum(1 for t in trades if t.get("outcome") == "LOSS")
        print(f"    {f.name:<30s} {count} trades | {wins}W-{losses}L | ${pnl:+.2f}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Analyze bot trading performance")
    parser.add_argument("--live", action="store_true", help="Analyze live trades")
    parser.add_argument("--all", action="store_true", help="Analyze both paper and live")
    parser.add_argument("--today", action="store_true", help="Today's trades only")
    parser.add_argument("--date", type=str, help="Specific date (YYYY-MM-DD)")
    parser.add_argument("--date-from", type=str, dest="date_from", help="Start date filter")
    parser.add_argument("--date-to", type=str, dest="date_to", help="End date filter")
    parser.add_argument("--days", action="store_true", help="List available trade files")
    parser.add_argument("--wipe-paper", action="store_true", help="Delete all paper data")
    args = parser.parse_args()

    if args.wipe_paper:
        confirm = input("Delete all paper trading data? (yes/no): ")
        if confirm.strip().lower() == "yes":
            wipe_paper()
        else:
            print("Cancelled.")
        return

    # Date filtering
    date_from = args.date_from or ""
    date_to = args.date_to or ""
    if args.today:
        from datetime import timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        date_from = today
        date_to = today
    elif args.date:
        date_from = args.date
        date_to = args.date

    mode = "live" if args.live else "paper"

    if args.days:
        list_days("paper")
        list_days("live")
        return

    if args.all:
        paper = load_trades("paper", date_from, date_to)
        live = load_trades("live", date_from, date_to)
        if paper:
            print_summary(paper, "paper")
        if live:
            print_summary(live, "live")
        if paper and live:
            print_summary(paper + live, "combined")
        if not paper and not live:
            print("\n  No trades found.\n")
    else:
        trades = load_trades(mode, date_from, date_to)
        label = f"{mode} ({date_from})" if date_from == date_to and date_from else mode
        print_summary(trades, label)


if __name__ == "__main__":
    main()
