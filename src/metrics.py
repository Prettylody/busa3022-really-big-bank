"""
Part 2 proof of concept: data curation and metrics.

Plain-language metric definitions (written BEFORE any code, and the spec given to the AI agent)

  M1  20-day rolling average volume
      The average number of index units traded over the last 20 ASX trading days, including today.
      Updated once per trading day. The first 19 days have no value (not enough history).
      Days the ASX did not trade are not rows, so they are never counted or filled in.
      Question it answers: is market activity above or below its recent normal level?

  M2  14-day volatility
      The standard deviation of daily percentage returns over the last 14 ASX trading days,
      where a daily return = (today's close / previous trading day's close) - 1.
      Shown as a percentage. Also shown annualised (x sqrt(252)) so it can be read next to the VIX.
      The first 14 days have no value.
      Question it answers: is the market calmer or more unsettled than usual?

Time axis (explicit time zone handling)
  - One row = one ASX trading day, dated in Australia/Sydney time.
  - The VIX is dated in US Eastern time. The US close for date D happens at about
    7-8 am Sydney time on D+1, which is AFTER the ASX has closed for D.
  - So each ASX day is matched to the most recent VIX close from a US date STRICTLY BEFORE it.
    Example: ASX Tuesday 14 Oct is paired with the VIX close from US Monday 13 Oct.
    Matching on the same calendar date would pair the ASX with a VIX value that had not
    happened yet when the ASX closed (look-ahead error).
  - US holidays: the latest earlier VIX value is carried forward, and vix_us_date shows which one.
"""

from __future__ import annotations

import math
import statistics
from pathlib import Path

import pandas as pd

VOLUME_WINDOW = 20
VOLATILITY_WINDOW = 14
TRADING_DAYS_PER_YEAR = 252
DASHBOARD_DAYS = 90
MAX_VIX_GAP_DAYS = 5  # a VIX value older than this is flagged as stale


def align(asx: pd.DataFrame, vix: pd.DataFrame) -> pd.DataFrame:
    """Join each ASX trading day to the latest VIX close from an earlier US date."""
    a = asx.copy()
    v = vix.copy()
    a["trade_date"] = pd.to_datetime(a["date"])
    v["vix_us_date"] = pd.to_datetime(v["date"])
    a = a.drop(columns="date").sort_values("trade_date")
    v = v.drop(columns="date").sort_values("vix_us_date")

    out = pd.merge_asof(
        a, v, left_on="trade_date", right_on="vix_us_date",
        direction="backward", allow_exact_matches=False,  # strictly earlier US date
    )
    gap = (out["trade_date"] - out["vix_us_date"]).dt.days
    out["vix_stale"] = gap.isna() | (gap > MAX_VIX_GAP_DAYS)
    return out.reset_index(drop=True)


def add_metrics(df: pd.DataFrame) -> pd.DataFrame:
    out = df.sort_values("trade_date").copy()
    out["vol_avg_20d"] = out["asx_volume"].rolling(VOLUME_WINDOW, min_periods=VOLUME_WINDOW).mean()
    out["daily_return_pct"] = out["asx_close"].pct_change() * 100
    out["volatility_14d_pct"] = out["daily_return_pct"].rolling(
        VOLATILITY_WINDOW, min_periods=VOLATILITY_WINDOW).std()  # sample std (n-1)
    out["volatility_14d_annual_pct"] = out["volatility_14d_pct"] * math.sqrt(TRADING_DAYS_PER_YEAR)
    return out.reset_index(drop=True)


def hand_check(df: pd.DataFrame, n_checks: int = 4) -> pd.DataFrame:
    """
    Recalculate both metrics for a few dates with plain Python (no pandas rolling),
    the same way you would in Excel, and compare with the pipeline's answer.
    This table is the Figure 3 evidence that the calculation logic is sound.
    """
    df = df.reset_index(drop=True)
    valid = df.index[df["volatility_14d_pct"].notna() & df["vol_avg_20d"].notna()]
    if len(valid) == 0:
        raise ValueError("Not enough history to check. Load at least 21 trading days.")
    picks = [valid[int(i * (len(valid) - 1) / max(n_checks - 1, 1))] for i in range(n_checks)]

    closes = df["asx_close"].tolist()
    volumes = df["asx_volume"].tolist()
    rows = []
    for i in sorted(set(picks)):
        manual_avg = sum(volumes[i - VOLUME_WINDOW + 1: i + 1]) / VOLUME_WINDOW
        rets = [(closes[j] / closes[j - 1] - 1) * 100 for j in range(i - VOLATILITY_WINDOW + 1, i + 1)]
        manual_vol = statistics.stdev(rets)
        rows.append({
            "trade_date": df.loc[i, "trade_date"].date(),
            "vol_avg_20d_code": round(df.loc[i, "vol_avg_20d"], 2),
            "vol_avg_20d_hand": round(manual_avg, 2),
            "volatility_14d_code": round(df.loc[i, "volatility_14d_pct"], 4),
            "volatility_14d_hand": round(manual_vol, 4),
            "match": (math.isclose(df.loc[i, "vol_avg_20d"], manual_avg, rel_tol=1e-9)
                      and math.isclose(df.loc[i, "volatility_14d_pct"], manual_vol, rel_tol=1e-9)),
        })
    return pd.DataFrame(rows)


def build_serve_table(df: pd.DataFrame, out_path: Path | None = None) -> pd.DataFrame:
    """The curated table the dashboard reads: last 90 trading days only."""
    serve = df.tail(DASHBOARD_DAYS).copy()
    serve["data_as_at"] = df["trade_date"].max().date()
    cols = ["trade_date", "asx_close", "asx_volume", "vol_avg_20d", "daily_return_pct",
            "volatility_14d_pct", "volatility_14d_annual_pct", "vix_us_date", "vix_close",
            "vix_stale", "data_as_at"]
    serve = serve[cols]
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        serve.to_csv(out_path, index=False)
    return serve


def run(data_dir: Path = Path("data")) -> pd.DataFrame:
    asx = pd.read_csv(data_dir / "clean" / "asx_daily.csv", dtype={"date": str})
    vix = pd.read_csv(data_dir / "clean" / "vix_daily.csv", dtype={"date": str})
    curated = add_metrics(align(asx, vix))
    curated.to_csv(data_dir / "clean" / "market_daily.csv", index=False)
    return build_serve_table(curated, data_dir / "serve" / "dashboard_90d.csv")


if __name__ == "__main__":
    print(run().tail(10).to_string(index=False))
