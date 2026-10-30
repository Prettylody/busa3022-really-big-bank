"""
Part 1 proof of concept: data pipeline for Really Big Bank market intelligence.

Layers
    Sources    -> FRED API (VIX, macro) and Yahoo Finance (S&P/ASX 200, market index)
    Ingest     -> fetch with a 30 second timeout and retries (failure scenario)
    Store raw  -> every response saved exactly as received, never edited
    Transform  -> validation rules split each batch into clean rows and rejected rows
    Quality    -> reject_log.csv (one line per bad record) and run_log.csv (one line per run)

The claim this script proves: no record is ever silently dropped.
For every source and every run:  rows_in == rows_passed + rows_already_loaded + rows_rejected
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

SYDNEY = ZoneInfo("Australia/Sydney")

# ---------------------------------------------------------------- settings
DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
RAW_DIR = DATA_DIR / "raw"
CLEAN_DIR = DATA_DIR / "clean"
LOG_DIR = DATA_DIR / "logs"

TIMEOUT_SECONDS = 30                 # a request slower than this counts as a timeout
RETRY_DELAYS_SECONDS = [60, 120, 240]  # wait 1, 2 then 4 minutes between attempts
LOOKBACK_DAYS = 16                   # about ten trading days, re-requested on every scheduled run
BACKFILL_DAYS = 200                  # first run only: about 140 trading days of history

FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
VIX_SERIES = "VIXCLS"
ASX_TICKER = "^AXJO"

REJECT_LOG = LOG_DIR / "reject_log.csv"
RUN_LOG = LOG_DIR / "run_log.csv"
ATTEMPT_LOG = LOG_DIR / "attempt_log.csv"
STALE_FLAG = CLEAN_DIR / "STALE.txt"


class SourceUnavailable(Exception):
    """Raised when every retry for a source has failed."""


# ---------------------------------------------------------------- helpers
def _now_sydney() -> datetime:
    return datetime.now(SYDNEY)


def _run_id() -> str:
    return _now_sydney().strftime("%Y%m%dT%H%M%S")


def _append_csv(path: Path, rows: list[dict]) -> None:
    """Append rows to a log file, writing the header only the first time."""
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, mode="a", header=not path.exists(), index=False)


def _setup_dirs() -> None:
    for d in (RAW_DIR, CLEAN_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- ingest with retries
def fetch_with_retry(fetch_fn, source: str, run_id: str, delays=None):
    """
    Call fetch_fn() and retry on timeout or error.
    Every attempt is logged (time, source, attempt number, outcome).
    After the last failure a SourceUnavailable error is raised.
    """
    delays = RETRY_DELAYS_SECONDS if delays is None else delays
    attempts = len(delays) + 1
    for attempt in range(1, attempts + 1):
        try:
            result = fetch_fn()
            _append_csv(ATTEMPT_LOG, [{
                "run_id": run_id, "attempt_time": _now_sydney().isoformat(timespec="seconds"),
                "source": source, "attempt": attempt, "outcome": "success", "error": "",
            }])
            return result
        except Exception as exc:  # timeout, connection error, HTTP error, empty response
            outcome = "timeout" if "timeout" in type(exc).__name__.lower() or "timed out" in str(exc).lower() else "error"
            _append_csv(ATTEMPT_LOG, [{
                "run_id": run_id, "attempt_time": _now_sydney().isoformat(timespec="seconds"),
                "source": source, "attempt": attempt, "outcome": outcome, "error": str(exc)[:200],
            }])
            if attempt < attempts:
                wait = delays[attempt - 1]
                print(f"  {source}: attempt {attempt} failed ({outcome}). Retrying in {wait}s")
                time.sleep(wait)
    raise SourceUnavailable(f"{source} failed after {attempts} attempts")


def fetch_vix(start: date, api_key: str) -> str:
    """Return the FRED API response body exactly as received (JSON text)."""
    params = {
        "series_id": VIX_SERIES,
        "api_key": api_key,
        "file_type": "json",
        "observation_start": start.isoformat(),
    }
    resp = requests.get(FRED_URL, params=params, timeout=TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.text


def fetch_asx(start: date) -> pd.DataFrame:
    """Return daily S&P/ASX 200 prices from Yahoo Finance exactly as received."""
    import yfinance as yf  # imported here so the rest of the file works without it

    df = yf.Ticker(ASX_TICKER).history(
        start=start.isoformat(), interval="1d", auto_adjust=False,
        timeout=TIMEOUT_SECONDS, raise_errors=True,
    )
    if df is None or df.empty:
        raise ValueError("Yahoo Finance returned no rows")
    return df


# ---------------------------------------------------------------- store raw (never edited)
def save_raw_vix(body: str, run_id: str) -> Path:
    path = RAW_DIR / f"fred_vixcls_{run_id}.json"
    path.write_text(body, encoding="utf-8")
    return path


def save_raw_asx(df: pd.DataFrame, run_id: str) -> Path:
    path = RAW_DIR / f"yahoo_axjo_{run_id}.csv"
    df.to_csv(path)
    return path


# ---------------------------------------------------------------- shape raw into records
def vix_records(body: str) -> pd.DataFrame:
    """Turn the FRED JSON into one row per observation. Values stay as text for now."""
    obs = json.loads(body).get("observations", [])
    return pd.DataFrame(
        [{"date": o.get("date"), "vix_close": o.get("value")} for o in obs],
        columns=["date", "vix_close"],
    )


def asx_records(df: pd.DataFrame) -> pd.DataFrame:
    """Turn the Yahoo frame into one row per trading day in Sydney time."""
    out = df.reset_index()
    date_col = out.columns[0]  # "Date", timezone-aware in Australia/Sydney
    dates = pd.to_datetime(out[date_col], errors="coerce", utc=True).dt.tz_convert(SYDNEY)
    return pd.DataFrame({
        "date": dates.dt.strftime("%Y-%m-%d"),
        "asx_close": out.get("Close"),
        "asx_volume": out.get("Volume"),
    })


# ---------------------------------------------------------------- transform: validation
def validate(records: pd.DataFrame, value_cols: list[str], source: str, run_id: str,
             already_loaded: set[str], positive_cols: list[str] | None = None):
    """
    Apply the quality rules in a fixed order. Every input row ends up in exactly one bucket.
        invalid_date        date missing or not a real calendar date
        non_numeric_value   a value is blank or text, e.g. FRED's "." for a missing day
        non_positive_value  a value that must be above zero is zero or negative (e.g. volume)
        duplicate_date      the same date appears twice in this batch
        already_loaded      the date is already in the clean layer (expected with the lookback window)
    Returns (passed, rejected, already_loaded_rows).
    """
    positive_cols = positive_cols or []
    df = records.copy()
    df["_reason"] = ""

    parsed = pd.to_datetime(df["date"], format="%Y-%m-%d", errors="coerce")
    df.loc[parsed.isna(), "_reason"] = "invalid_date"
    df["_date"] = parsed.dt.strftime("%Y-%m-%d")

    for col in value_cols:
        num = pd.to_numeric(df[col], errors="coerce")
        bad = (df["_reason"] == "") & num.isna()
        df.loc[bad, "_reason"] = "non_numeric_value"
        df[col + "_num"] = num

    for col in positive_cols:
        bad = (df["_reason"] == "") & (df[col + "_num"] <= 0)
        df.loc[bad, "_reason"] = "non_positive_value"

    ok = df["_reason"] == ""
    dup = ok & df["_date"].duplicated(keep="first")
    df.loc[dup, "_reason"] = "duplicate_date"

    ok = df["_reason"] == ""
    seen = ok & df["_date"].isin(already_loaded)
    df.loc[seen, "_reason"] = "already_loaded"

    passed = df[df["_reason"] == ""]
    passed = pd.DataFrame(
        {"date": passed["_date"], **{c: passed[c + "_num"] for c in value_cols}}
    ).reset_index(drop=True)
    already = df[df["_reason"] == "already_loaded"]
    rejected = df[~df["_reason"].isin(["", "already_loaded"])]

    reject_rows = [{
        "run_id": run_id, "source": source,
        "record_date": r["date"], "raw_values": "|".join(str(r[c]) for c in value_cols),
        "rule_failed": r["_reason"], "action_taken": "rejected",
    } for _, r in rejected.iterrows()]

    return passed, reject_rows, len(already)


# ---------------------------------------------------------------- clean layer
def load_clean(name: str) -> pd.DataFrame:
    path = CLEAN_DIR / f"{name}.csv"
    if path.exists():
        return pd.read_csv(path, dtype={"date": str})
    return pd.DataFrame(columns=["date"])


def append_clean(name: str, rows: pd.DataFrame) -> None:
    existing = load_clean(name)
    combined = pd.concat([existing, rows], ignore_index=True) if len(existing) else rows
    combined = combined.sort_values("date").reset_index(drop=True)
    combined.to_csv(CLEAN_DIR / f"{name}.csv", index=False)


# ---------------------------------------------------------------- one source, end to end
def process_source(source: str, clean_name: str, fetch_fn, save_raw_fn, to_records_fn,
                   value_cols, run_id: str, positive_cols=None, delays=None) -> dict:
    started = _now_sydney()
    try:
        raw = fetch_with_retry(fetch_fn, source, run_id, delays)
    except SourceUnavailable as exc:
        # Failure plan: keep the last good load, flag the data as stale, alert, log
        STALE_FLAG.write_text(
            f"{source} unavailable at {started.isoformat(timespec='seconds')}. "
            f"Dashboard is showing the last good load.\n", encoding="utf-8")
        summary = {"run_id": run_id, "run_time": started.isoformat(timespec="seconds"),
                   "source": source, "status": "failed_kept_last_good", "raw_file": "",
                   "rows_in": 0, "rows_passed": 0, "rows_already_loaded": 0,
                   "rows_rejected": 0, "reconciles": True, "note": str(exc)}
        _append_csv(RUN_LOG, [summary])
        print(f"ALERT: {exc}. Last good load kept and data flagged as stale.")
        return summary

    raw_path = save_raw_fn(raw, run_id)
    records = to_records_fn(raw)
    already = set(load_clean(clean_name)["date"].astype(str))
    passed, reject_rows, n_already = validate(records, value_cols, source, run_id,
                                              already, positive_cols)
    append_clean(clean_name, passed)
    _append_csv(REJECT_LOG, reject_rows)

    rows_in = len(records)
    reconciles = rows_in == len(passed) + n_already + len(reject_rows)
    summary = {"run_id": run_id, "run_time": started.isoformat(timespec="seconds"),
               "source": source, "status": "success", "raw_file": raw_path.name,
               "rows_in": rows_in, "rows_passed": len(passed),
               "rows_already_loaded": n_already, "rows_rejected": len(reject_rows),
               "reconciles": reconciles, "note": ""}
    _append_csv(RUN_LOG, [summary])
    if not reconciles:
        raise RuntimeError(f"{source}: row counts do not reconcile. Records were lost.")
    return summary


def run(fred_api_key: str, start: date | None = None, delays=None) -> pd.DataFrame:
    """
    Run the pipeline once for both sources.
    start=None uses the scheduled lookback window. Pass a date to backfill history.
    Returns the run summary (this is the Figure 2 table).
    """
    _setup_dirs()
    run_id = _run_id()
    if start is None:
        first_run = load_clean("vix_daily").empty or load_clean("asx_daily").empty
        # First ever run: backfill enough history for the 90-day dashboard plus the 20-day window
        days_back = BACKFILL_DAYS if first_run else LOOKBACK_DAYS
        start = _now_sydney().date() - timedelta(days=days_back)
    print(f"Run {run_id}, requesting data from {start}")

    results = [
        process_source("FRED VIXCLS", "vix_daily",
                       lambda: fetch_vix(start, fred_api_key), save_raw_vix, vix_records,
                       ["vix_close"], run_id, delays=delays),
        process_source("Yahoo ^AXJO", "asx_daily",
                       lambda: fetch_asx(start), save_raw_asx, asx_records,
                       ["asx_close", "asx_volume"], run_id,
                       positive_cols=["asx_close", "asx_volume"], delays=delays),
    ]
    if all(r["status"] == "success" for r in results) and STALE_FLAG.exists():
        STALE_FLAG.unlink()  # fresh data loaded, clear the stale flag
    return pd.DataFrame(results)


if __name__ == "__main__":
    key = os.environ.get("FRED_API_KEY")
    if not key:
        raise SystemExit("Set the FRED_API_KEY environment variable first.")
    summary = run(key)
    print(summary.to_string(index=False))
    if (summary["status"] != "success").any():
        raise SystemExit(1)  # a failed job makes GitHub Actions email the owner (the alert)
