# coding=utf-8
"""Run TrendRadar with ``python -m trendradar``."""

from trendradar.cli import main
from trendradar.daily import NewsAnalyzer, _is_manual_force_run

__all__ = ["NewsAnalyzer", "_is_manual_force_run", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
