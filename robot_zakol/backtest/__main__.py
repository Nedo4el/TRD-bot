"""CLI: python -m robot_zakol.backtest --symbol BTRUSDT --start-date ... --end-date ..."""

from __future__ import annotations

import argparse
import asyncio

from core.bybit_client import BybitClient
from core.config import Config
from core.logger import setup_logging
from robot_zakol.backtest.backtest import run_modes
from robot_zakol.backtest.data_loader import (
    bars_for_range,
    end_of_day_ms,
    load_series,
    parse_ms,
)
from robot_zakol.backtest.report import build_report, write_report
from robot_zakol.config import load_config


async def _amain(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Honest backtest robot_zakol (tick/1s/1m, 3 fill models)",
    )
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--interval", default="1", help="kline interval (best effort)")
    parser.add_argument("--deposit", type=float, default=100.0)
    parser.add_argument(
        "--modes",
        default="ideal,realistic,pessimistic",
        help="comma list: ideal,realistic,pessimistic",
    )
    parser.add_argument("--report", default=None)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)

    zakol = load_config()
    setup_logging(zakol.log_file, zakol.log_level)
    symbol = args.symbol or zakol.symbol
    start_ms = parse_ms(args.start_date)
    end_ms = end_of_day_ms(args.end_date)
    limit = bars_for_range(start_ms, end_ms, args.interval)

    core = Config(
        api_key=zakol.api_key,
        api_secret=zakol.api_secret,
        testnet=zakol.testnet,
        simulation_mode=True,
        category=zakol.category,
        symbol=symbol,
        requests_per_second=zakol.requests_per_second,
        ws_enabled=False,
    )
    client = BybitClient(core)
    try:
        series = await load_series(
            client,
            symbol,
            args.interval,
            limit,
            start_ms,
            end_ms,
        )
        modes = tuple(m.strip() for m in args.modes.split(",") if m.strip())
        results = run_modes(series, deposit=args.deposit, modes=modes)
        text = build_report(
            symbol=symbol,
            start=args.start_date,
            end=args.end_date,
            deposit=args.deposit,
            results=results,
            source_note=series.source_note,
        )
        print(text)
        if args.report:
            path = write_report(text, args.report)
            print(f"report: {path}")
    finally:
        client.close()


def main(argv: list[str] | None = None) -> None:
    asyncio.run(_amain(argv))


if __name__ == "__main__":
    main()
