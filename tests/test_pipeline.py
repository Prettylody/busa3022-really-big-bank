"""
Tests that feed known bad rows through the pipeline.
Run with:  pytest -q
No internet or API key needed. The sources are replaced with fake responses.
"""

import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import metrics  # noqa: E402
import pipeline  # noqa: E402


@pytest.fixture(autouse=True)
def temp_data_dir(tmp_path, monkeypatch):
    """Point every test at its own empty data folder."""
    monkeypatch.setattr(pipeline, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(pipeline, "CLEAN_DIR", tmp_path / "clean")
    monkeypatch.setattr(pipeline, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(pipeline, "REJECT_LOG", tmp_path / "logs" / "reject_log.csv")
    monkeypatch.setattr(pipeline, "RUN_LOG", tmp_path / "logs" / "run_log.csv")
    monkeypatch.setattr(pipeline, "ATTEMPT_LOG", tmp_path / "logs" / "attempt_log.csv")
    monkeypatch.setattr(pipeline, "STALE_FLAG", tmp_path / "clean" / "STALE.txt")
    pipeline._setup_dirs()
    return tmp_path


def fred_body(rows):
    return json.dumps({"observations": [{"date": d, "value": v} for d, v in rows]})


# ------------------------------------------------------------- validation rules
def test_known_bad_vix_rows_are_rejected_with_reasons():
    records = pipeline.vix_records(fred_body([
        ("2026-10-13", "19.03"),
        ("2026-10-14", "."),          # FRED's marker for a missing day
        ("2026-13-40", "18.50"),      # impossible date
        ("2026-10-15", "18.71"),
        ("2026-10-15", "18.71"),      # duplicate date
        ("2026-10-16", "abc"),        # text instead of a number
    ]))
    passed, rejects, already = pipeline.validate(records, ["vix_close"], "FRED", "t", set())
    reasons = sorted(r["rule_failed"] for r in rejects)
    assert list(passed["date"]) == ["2026-10-13", "2026-10-15"]
    assert reasons == ["duplicate_date", "invalid_date", "non_numeric_value", "non_numeric_value"]
    assert len(records) == len(passed) + len(rejects) + already  # nothing silently dropped


def test_zero_volume_is_rejected_for_asx():
    records = pd.DataFrame({"date": ["2026-10-13", "2026-10-14"],
                            "asx_close": [9000.0, 9010.0], "asx_volume": [5e8, 0]})
    passed, rejects, _ = pipeline.validate(records, ["asx_close", "asx_volume"], "Yahoo", "t",
                                           set(), positive_cols=["asx_volume"])
    assert len(passed) == 1 and rejects[0]["rule_failed"] == "non_positive_value"


def test_rerun_is_idempotent(monkeypatch):
    body = fred_body([("2026-10-13", "19.0"), ("2026-10-14", "."), ("2026-10-15", "18.0")])
    args = dict(source="FRED VIXCLS", clean_name="vix_daily", fetch_fn=lambda: body,
                save_raw_fn=pipeline.save_raw_vix, to_records_fn=pipeline.vix_records,
                value_cols=["vix_close"], delays=[])
    first = pipeline.process_source(run_id="r1", **args)
    second = pipeline.process_source(run_id="r2", **args)
    assert (first["rows_passed"], first["rows_rejected"]) == (2, 1)
    assert (second["rows_passed"], second["rows_already_loaded"], second["rows_rejected"]) == (0, 2, 1)
    assert len(pipeline.load_clean("vix_daily")) == 2  # no duplicates in the clean layer
    assert first["reconciles"] and second["reconciles"]


def test_first_run_backfills_history_then_uses_short_lookback(monkeypatch):
    seen = []
    monkeypatch.setattr(pipeline, "fetch_vix", lambda start, key: (seen.append(start), fred_body([("2026-10-13", "19.0")]))[1])
    monkeypatch.setattr(pipeline, "fetch_asx", lambda start: (seen.append(start), pd.DataFrame(
        {"Close": [9000.0], "Volume": [5e8]},
        index=pd.DatetimeIndex(["2026-10-13"], name="Date").tz_localize("Australia/Sydney")))[1])
    pipeline.run("key", delays=[])
    pipeline.run("key", delays=[])
    today = pipeline._now_sydney().date()
    assert (today - seen[0]).days == pipeline.BACKFILL_DAYS
    assert (today - seen[2]).days == pipeline.LOOKBACK_DAYS


# ------------------------------------------------------------- failure scenario
def test_timeout_retries_then_keeps_last_good_load():
    import requests
    calls = {"n": 0}

    def always_times_out():
        calls["n"] += 1
        raise requests.Timeout("Read timed out after 30s")

    pipeline.append_clean("vix_daily", pd.DataFrame({"date": ["2026-10-13"], "vix_close": [19.0]}))
    summary = pipeline.process_source("FRED VIXCLS", "vix_daily", always_times_out,
                                      pipeline.save_raw_vix, pipeline.vix_records,
                                      ["vix_close"], "r1", delays=[0, 0, 0])
    attempts = pd.read_csv(pipeline.ATTEMPT_LOG)
    assert calls["n"] == 4                                   # first try + 3 retries
    assert list(attempts["outcome"]) == ["timeout"] * 4
    assert summary["status"] == "failed_kept_last_good"
    assert pipeline.STALE_FLAG.exists()
    assert len(pipeline.load_clean("vix_daily")) == 1      # last good load untouched


def test_recovers_if_a_retry_succeeds():
    import requests
    state = {"n": 0}

    def fails_once():
        state["n"] += 1
        if state["n"] == 1:
            raise requests.Timeout("timed out")
        return fred_body([("2026-10-13", "19.0")])

    summary = pipeline.process_source("FRED VIXCLS", "vix_daily", fails_once,
                                      pipeline.save_raw_vix, pipeline.vix_records,
                                      ["vix_close"], "r1", delays=[0, 0, 0])
    assert summary["status"] == "success" and summary["rows_passed"] == 1


# ------------------------------------------------------------- time zone alignment
def test_asx_day_uses_previous_us_vix_close():
    asx = pd.DataFrame({"date": ["2026-10-13", "2026-10-14"],
                        "asx_close": [1, 1], "asx_volume": [1, 1]})
    vix = pd.DataFrame({"date": ["2026-10-10", "2026-10-13", "2026-10-14"],
                        "vix_close": [20.0, 21.0, 22.0]})
    out = metrics.align(asx, vix)
    # ASX Mon 13 Oct closes before the US Mon 13 Oct session, so it gets Fri 10 Oct
    assert list(out["vix_us_date"].dt.strftime("%Y-%m-%d")) == ["2026-10-10", "2026-10-13"]
    assert list(out["vix_close"]) == [20.0, 21.0]


# ------------------------------------------------------------- metrics
def test_metrics_match_hand_calculation():
    rng = np.random.default_rng(1)
    n = 60
    dates = pd.bdate_range("2026-06-01", periods=n)
    asx = pd.DataFrame({"date": dates.strftime("%Y-%m-%d"),
                        "asx_close": 8800 * np.cumprod(1 + rng.normal(0, 0.008, n)),
                        "asx_volume": rng.integers(4e8, 9e8, n).astype(float)})
    vix = pd.DataFrame({"date": dates.strftime("%Y-%m-%d"), "vix_close": rng.uniform(13, 25, n)})
    df = metrics.add_metrics(metrics.align(asx, vix))
    assert df["vol_avg_20d"].iloc[:19].isna().all() and df["vol_avg_20d"].iloc[19:].notna().all()
    assert df["volatility_14d_pct"].iloc[:14].isna().all()
    assert metrics.hand_check(df)["match"].all()
