#!/usr/bin/env python3
"""scripts/acceptance.py — CLI-обёртка над приёмочным стендом H2 (§21).

Использование:
    python scripts/acceptance.py                 # только AUTO-критерии
    python scripts/acceptance.py --live           # + LIVE (нужно живое железо)
    python scripts/acceptance.py --only 7,15      # выборочно
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.acceptance.harness import report_markdown, run_all


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--live",
        action="store_true",
        help="прогнать LIVE-критерии (нужно живое железо и человек)",
    )
    ap.add_argument(
        "--only",
        type=str,
        default=None,
        help="список номеров критериев через запятую, напр. 7,15",
    )
    args = ap.parse_args()

    only = {int(x) for x in args.only.split(",")} if args.only else None
    results = asyncio.run(run_all(include_live=args.live, only=only))
    report = report_markdown(results)
    print(report)

    out_path = Path(f"acceptance_report_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.md")
    out_path.write_text(report, encoding="utf-8")
    print(f"отчёт сохранён: {out_path}")

    any_failed = any(r.passed is False for r in results)
    return 1 if any_failed else 0


if __name__ == "__main__":
    sys.exit(main())
