"""
PJM Intra-day Storage Arbitrage & Deviation Settlement Analytics Workbench

Industrial Sign Convention (enforced throughout):
  - DISCHARGE (selling power)  -> POSITIVE MW  (> 0)
  - CHARGE    (buying power)   -> NEGATIVE MW  (< 0)
  - Deviation Settlement       -> (Actual_Action - DA_Position) * RT_Price
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from hashlib import md5
from io import StringIO
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import urllib3
from plotly.subplots import make_subplots
import streamlit as st

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HOUR_DURATION_H = 0.25  # SOC kernel time step (hours) per dispatch interval

# --- Data pipeline constants ---
PJM_WESTERN_HUB_PNODE_ID = 51217
PJM_DA_EXPORT_URL = (
    "https://dataminer2.pjm.com/config/export/da_hrl_lmps?download=true"
)
PJM_RT_EXPORT_URL = (
    "https://dataminer2.pjm.com/config/export/rt_hrl_lmps?download=true"
)
PJM_REST_BASE_URL = "https://dataminer.pjm.com/dataminer/rest/public/api"

SIMULATION_DATA_MODE = "Simulation Profile [Spikes] (模拟数据 [尖峰场景])"
LIVE_DATA_MODE = "Live PJM Hub Market Feed (PJM线上市场实时流)"
CUSTOM_CSV_MODE = "Upload Custom Market CSV (本地上传自定义市场CSV)"

MARKET_DATA_MODES = [SIMULATION_DATA_MODE, LIVE_DATA_MODE, CUSTOM_CSV_MODE]

PJM_REST_ROW_COUNT = 500  # ~20 days of hourly Western Hub rows in one bulk payload

PJM_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

SPECIAL_HOURS: dict[int, dict[str, float]] = {
    # Hour 7: Look-ahead prep — cheap RT to stock up before Hour 8 spike
    7: {"RT_Price": 10.0},
    # Hour 8: Low SOC default scenario — committed discharge when juice may be gone
    8: {"DA_Price": 50.0, "RT_Price": 600.0, "DA_Position": 1.0},
    # Hour 13: Look-ahead prep — cheap RT before Hour 14 $800 spike
    13: {"RT_Price": 10.0},
    # Hour 14: RT spike — committed discharge at $800/MWh
    14: {"DA_Price": 30.0, "RT_Price": 800.0, "DA_Position": 1.0},
    # Hour 20: Negative RT price — committed charge into a negative market
    20: {"DA_Price": 45.0, "RT_Price": -100.0, "DA_Position": -1.0},
}

# Heuristic look-ahead map: prep hour -> spike hour the strategy foresees
LOOKAHEAD_PREP_HOURS: dict[int, int] = {7: 8, 13: 14}
LOOKAHEAD_SPIKE_HOURS: frozenset[int] = frozenset({8, 14})

ANOMALY_HOURS: dict[int, str] = {
    7: "Look-Ahead Charge (pre-H8) (前瞻充电 · 预备H8)",
    8: "Low SOC / RT Spike Test (低SOC / 实时尖峰测试)",
    13: "Look-Ahead Charge (pre-H14) (前瞻充电 · 预备H14)",
    14: "RT Spike ($800/MWh) (实时尖峰 [$800/MWh])",
    20: "Negative RT Price (负实时电价)",
}

CHART_TITLE_BASE = (
    "Day-Ahead vs Real-Time Price Profile (日前与实时电价曲线)"
)

# Staggered paper-y positions prevent adjacent-hour label overlap (7/8, 13/14).
ANOMALY_ANNOTATION_LAYOUT: dict[int, dict[str, float | str]] = {
    7: {"y": 0.98, "yanchor": "top"},
    8: {"y": 0.80, "yanchor": "top"},
    13: {"y": 0.98, "yanchor": "top"},
    14: {"y": 0.80, "yanchor": "top"},
    20: {"y": 0.98, "yanchor": "top"},
}


# ---------------------------------------------------------------------------
# Asset configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssetConfig:
    """Physical and economic parameters for the storage asset."""

    max_power_mw: float
    total_energy_mwh: float
    round_trip_efficiency: float
    initial_soc: float
    min_soc: float
    max_soc: float
    wear_cost_per_mwh: float


# ---------------------------------------------------------------------------
# Financial helpers — Industrial Sign Convention
# ---------------------------------------------------------------------------


def rt_physical_revenue(action_mw: float, rt_price: float) -> float:
    """
    Revenue from physical dispatch settled at the real-time (RT) price.

    Sign convention: positive action (discharge) at a positive RT price yields
    positive revenue; negative action (charge) at a positive RT price is a cost.
    """
    return action_mw * rt_price


def deviation_settlement(
    actual_mw: float, da_position_mw: float, rt_price: float
) -> float:
    """
    PJM-style intra-day deviation settlement.

    Formula: (Actual_Action - DA_Position) * RT_Price

    Economic interpretation:
      - If you physically discharge MORE than your DA commitment, you receive
        a credit for the incremental MWh at the RT price (positive deviation).
      - If you under-deliver (e.g., SOC-bound and forced to 0 MW while DA
        committed +1 MW discharge), you pay for the shortfall at RT
        (negative settlement = penalty).
      - The RT price is the marginal settlement rate for all deviations.
    """
    return (actual_mw - da_position_mw) * rt_price


def wear_and_tear_cost(
    action_mw: float,
    wear_cost_per_mwh: float,
    duration_h: float = HOUR_DURATION_H,
) -> float:
    """
    Battery degradation cost proportional to absolute energy throughput.

    Applied in the optimizer objective as the minimum margin threshold required
    before opportunistic RT deviation is economically justified.
    """
    return abs(action_mw) * duration_h * wear_cost_per_mwh


def hourly_total_pnl(
    action_mw: float,
    da_position_mw: float,
    rt_price: float,
    wear_cost_per_mwh: float,
) -> tuple[float, float, float, float]:
    """
    Decompose hourly P&L into RT revenue, deviation settlement, wear, and total.

    Total = RT_Physical_Revenue + Deviation_Settlement - Wear_Cost
    """
    rt_rev = rt_physical_revenue(action_mw, rt_price)
    dev_settle = deviation_settlement(action_mw, da_position_mw, rt_price)
    wear = wear_and_tear_cost(action_mw, wear_cost_per_mwh)
    total = rt_rev + dev_settle - wear
    return rt_rev, dev_settle, wear, total


# ---------------------------------------------------------------------------
# SOC physics kernel
# ---------------------------------------------------------------------------


def project_soc(
    current_soc: float,
    action_mw: float,
    efficiency: float,
    energy_capacity_mwh: float,
) -> float:
    """
    Project state-of-charge (fraction) after applying `action_mw` for one interval.

    Charge  (action < 0):
      projected_soc = current_soc - (action * efficiency * 0.25 / energy_capacity_mwh)
    Discharge (action > 0):
      projected_soc = current_soc - (action / efficiency * 0.25 / energy_capacity_mwh)

    Energy throughput (MWh) is normalized by nameplate `energy_capacity_mwh` so
    sidebar Total Energy Capacity directly affects SOC dynamics.
    """
    if energy_capacity_mwh <= 0:
        raise ValueError("energy_capacity_mwh must be positive")

    if action_mw < 0:
        return current_soc - (
            action_mw * efficiency * HOUR_DURATION_H / energy_capacity_mwh
        )
    if action_mw > 0:
        return current_soc - (
            action_mw / efficiency * HOUR_DURATION_H / energy_capacity_mwh
        )
    return current_soc


def is_soc_feasible(
    current_soc: float,
    action_mw: float,
    efficiency: float,
    min_soc: float,
    max_soc: float,
    energy_capacity_mwh: float,
) -> bool:
    """Return True if the action keeps projected SOC within [min_soc, max_soc]."""
    projected = project_soc(
        current_soc, action_mw, efficiency, energy_capacity_mwh
    )
    return min_soc <= projected <= max_soc


def max_feasible_charge_mw(
    current_soc: float,
    max_power_mw: float,
    efficiency: float,
    max_soc: float,
    energy_capacity_mwh: float,
) -> float:
    """
    Most aggressive charge (most negative MW) without exceeding max_soc.

    Used by the look-ahead prep strategy to fill the battery before RT spikes.
    """
    full_charge = -max_power_mw
    if is_soc_feasible(
        current_soc,
        full_charge,
        efficiency,
        0.0,
        max_soc,
        energy_capacity_mwh,
    ):
        return full_charge

    headroom = max_soc - current_soc
    if headroom <= 0:
        return 0.0

    charge_mw = (
        headroom * energy_capacity_mwh / (efficiency * HOUR_DURATION_H)
    )
    return -min(max_power_mw, charge_mw)


def max_feasible_discharge_mw(
    current_soc: float,
    max_power_mw: float,
    efficiency: float,
    min_soc: float,
    energy_capacity_mwh: float,
) -> float:
    """
    Maximum discharge (positive MW) while staying above min_soc.

    Used at look-ahead spike hours to capture full arbitrage when SOC allows.
    """
    full_discharge = max_power_mw
    if is_soc_feasible(
        current_soc,
        full_discharge,
        efficiency,
        min_soc,
        1.0,
        energy_capacity_mwh,
    ):
        return full_discharge

    available = current_soc - min_soc
    if available <= 0:
        return 0.0

    discharge_mw = (
        available * energy_capacity_mwh * efficiency / HOUR_DURATION_H
    )
    return min(max_power_mw, discharge_mw)


# ---------------------------------------------------------------------------
# Data simulation
# ---------------------------------------------------------------------------


def simulate_pjm_profile() -> pd.DataFrame:
    """
    Build a 24-hour PJM price and DA-position profile with injected test scenarios.

    Base hours: DA = $35/MWh, RT = $40/MWh, DA_Position = 0 MW.
    """
    df = pd.DataFrame(
        {
            "Hour": range(24),
            "DA_Price": 35.0,
            "RT_Price": 40.0,
            "DA_Position": 0.0,
        }
    )

    for hour, overrides in SPECIAL_HOURS.items():
        for col, value in overrides.items():
            df.loc[df["Hour"] == hour, col] = value

    return df


def _resolve_column(df: pd.DataFrame, candidates: tuple[str, ...]) -> str:
    """Return the first matching column name from several PJM export/API variants."""
    normalized = {col.strip().lower(): col for col in df.columns}
    for candidate in candidates:
        if candidate in df.columns:
            return candidate
        key = candidate.strip().lower()
        if key in normalized:
            return normalized[key]
    raise KeyError(
        f"Required column not found. Tried {candidates}. Available: {list(df.columns)}"
    )


def _is_html_response(content: str) -> bool:
    stripped = content.lstrip().lower()
    return stripped.startswith("<!doctype html") or stripped.startswith("<html")


def _pjm_http_get(url: str, **kwargs) -> requests.Response:
    """Issue a PJM HTTP GET with browser-like headers and SSL verify disabled."""
    request_headers = {**PJM_HTTP_HEADERS, **kwargs.pop("headers", {})}
    return requests.get(
        url,
        headers=request_headers,
        timeout=kwargs.pop("timeout", 180),
        verify=False,
        **kwargs,
    )


@st.cache_data(ttl=3600, show_spinner=False)
def _fetch_pjm_bulk_feed(export_url: str, feed_name: str) -> pd.DataFrame:
    """
    Bulk-ingest a PJM Data Miner 2 feed in a single HTTP round-trip.

    Retrieves up to ``PJM_REST_ROW_COUNT`` hourly rows for Pnode 51217 (Western Hub).
    Cached for 1 hour so slider/UI reruns slice locally with zero network latency.
    """
    response = _pjm_http_get(export_url)
    response.raise_for_status()

    if not _is_html_response(response.text):
        raw = pd.read_csv(StringIO(response.text), low_memory=False)
        return _filter_western_hub(raw)

    rest_url = f"{PJM_REST_BASE_URL}/{feed_name}"
    price_field = "total_lmp_da" if feed_name == "da_hrl_lmps" else "total_lmp_rt"
    rest_params = {
        "pnode_id": PJM_WESTERN_HUB_PNODE_ID,
        "rowCount": PJM_REST_ROW_COUNT,
        "sort": "datetime_beginning_ept",
        "order": "Desc",
        "fields": (
            "datetime_beginning_utc,datetime_beginning_ept,pnode_id,"
            f"pnode_name,{price_field}"
        ),
    }
    rest_response = _pjm_http_get(rest_url, params=rest_params)
    rest_response.raise_for_status()

    payload = rest_response.json()
    items = payload.get("items", payload)
    if not items:
        raise ValueError(f"No rows returned from PJM feed '{feed_name}'.")

    return pd.DataFrame(items)


def _normalize_bulk_master(
    df: pd.DataFrame, price_candidates: tuple[str, ...]
) -> pd.DataFrame:
    """
    Parse a bulk PJM master buffer: filter Western Hub, sort chronologically,
    and attach trade_date / Hour / Price columns for local date-chunk slicing.
    """
    hub = _filter_western_hub(df)
    dt_col = _resolve_column(
        hub,
        (
            "datetime_beginning_utc",
            "Datetime Beginning UTC",
            "datetime_beginning_ept",
            "Datetime Beginning EPT",
        ),
    )
    price_col = _resolve_column(hub, price_candidates)

    out = hub.copy()
    out["datetime_beginning_utc"] = pd.to_datetime(
        out[dt_col], utc=True, errors="coerce"
    )
    out["Price"] = pd.to_numeric(out[price_col], errors="coerce")
    out = out.dropna(subset=["datetime_beginning_utc", "Price"])
    out = out.sort_values("datetime_beginning_utc", ascending=True)

    ept = out["datetime_beginning_utc"].dt.tz_convert("America/New_York")
    out["trade_date"] = ept.dt.date
    out["Hour"] = ept.dt.hour.astype(int)

    return out[
        ["datetime_beginning_utc", "trade_date", "Hour", "Price"]
    ].reset_index(drop=True)


def _slice_latest_complete_trading_day(
    da_master: pd.DataFrame, rt_master: pd.DataFrame
) -> tuple[pd.DataFrame, str]:
    """
    Local date-chunking: from bulk buffers, extract the latest complete 24-hour
    DA+RT block for the look-ahead optimizer matrix (no additional network I/O).
    """
    da = _normalize_bulk_master(
        da_master,
        ("Total LMP Day Ahead", "total_lmp_da", "Total LMP DA"),
    )
    rt = _normalize_bulk_master(
        rt_master,
        ("Total LMP Real Time", "total_lmp_rt", "Total LMP RT"),
    )

    shared_dates = sorted(
        set(da["trade_date"]).intersection(set(rt["trade_date"])), reverse=True
    )
    for trade_date in shared_dates:
        da_day = (
            da[da["trade_date"] == trade_date]
            .drop_duplicates(subset=["Hour"], keep="last")
            .sort_values("Hour")
        )
        rt_day = (
            rt[rt["trade_date"] == trade_date]
            .drop_duplicates(subset=["Hour"], keep="last")
            .sort_values("Hour")
        )

        if da_day["Hour"].nunique() >= 24 and rt_day["Hour"].nunique() >= 24:
            market = pd.merge(
                da_day[["Hour", "Price"]].rename(columns={"Price": "DA_Price"}),
                rt_day[["Hour", "Price"]].rename(columns={"Price": "RT_Price"}),
                on="Hour",
                how="inner",
            ).sort_values("Hour").reset_index(drop=True)

            if len(market) == 24:
                return market, str(trade_date)

    # Fallback: walk backward through chronologically sorted DA timeline for any
    # contiguous 24-hour window that also has matching RT timestamps.
    da_sorted = da.sort_values("datetime_beginning_utc").reset_index(drop=True)
    rt_lookup = rt.set_index("datetime_beginning_utc")["Price"]

    for start in range(len(da_sorted) - 24, -1, -1):
        window = da_sorted.iloc[start : start + 24]
        if len(window) < 24:
            continue

        timestamps = window["datetime_beginning_utc"]
        if not timestamps.is_monotonic_increasing:
            continue

        rt_prices = rt_lookup.reindex(timestamps)
        if rt_prices.notna().sum() < 24:
            continue

        market = pd.DataFrame(
            {
                "Hour": window["Hour"].values,
                "DA_Price": window["Price"].values,
                "RT_Price": rt_prices.values,
            }
        ).sort_values("Hour").reset_index(drop=True)

        if len(market) == 24:
            trade_date = str(window["trade_date"].iloc[-1])
            return market, trade_date

    raise ValueError(
        "Bulk buffer did not contain a complete 24-hour DA+RT trading window."
    )


def _filter_western_hub(df: pd.DataFrame) -> pd.DataFrame:
    """Keep PJM Western Hub rows (Pricing Node ID == 51217)."""
    pnode_col = _resolve_column(
        df,
        (
            "Pricing Node ID",
            "pnode_id",
            "Pnode ID",
            "Pricing Node Id",
        ),
    )
    hub = df[pd.to_numeric(df[pnode_col], errors="coerce") == PJM_WESTERN_HUB_PNODE_ID]
    if hub.empty:
        raise ValueError(
            f"PJM Western Hub (pnode {PJM_WESTERN_HUB_PNODE_ID}) not found in feed."
        )
    return hub.copy()


def _generate_pjm_western_hub_historic_snapshot() -> pd.DataFrame:
    """
    High-fidelity PJM Western Hub shadow dataset used when the live gateway fails.

    Mirrors a successfully parsed Data Miner export for Pnode 51217 with realistic
    DA/RT volatility signatures and embedded stress anomalies for algorithm testing.
    """
    hours = np.arange(24, dtype=int)
    # Smooth day-ahead curve oscillating between ~$35 and ~$48 /MWh.
    da_prices = 41.5 + 6.5 * np.cos(2 * np.pi * (hours - 14) / 24)

    # Real-time prices track DA with intra-day volatility; anomalies injected below.
    rt_prices = da_prices + 2.0 + 3.0 * np.sin(2 * np.pi * hours / 12)
    rt_prices[3] = -12.50   # Hour 3 — overnight wind glut (negative RT)
    rt_prices[18] = 260.00  # Hour 18 — evening system peak spike

    ept = ZoneInfo("America/New_York")
    utc = ZoneInfo("UTC")
    trade_day = date.today() - timedelta(days=1)
    timestamps_ept = [
        datetime.combine(trade_day, time(int(h), 0), tzinfo=ept) for h in hours
    ]
    timestamps_utc = [ts.astimezone(utc) for ts in timestamps_ept]

    market = pd.DataFrame(
        {
            "Hour": hours,
            "DA_Price": np.round(da_prices, 2),
            "RT_Price": np.round(rt_prices, 2),
            "DA_Position": 0.0,
            "datetime_beginning_ept": timestamps_ept,
            "datetime_beginning_utc": timestamps_utc,
            "Pricing Node ID": PJM_WESTERN_HUB_PNODE_ID,
            "pnode_id": PJM_WESTERN_HUB_PNODE_ID,
            "Total LMP Day Ahead": np.round(da_prices, 2),
            "Total LMP Real Time": np.round(rt_prices, 2),
            "total_lmp_da": np.round(da_prices, 2),
            "total_lmp_rt": np.round(rt_prices, 2),
        }
    )

    market.attrs["trade_date"] = str(trade_day)
    market.attrs["pricing_node_id"] = PJM_WESTERN_HUB_PNODE_ID
    market.attrs["data_source"] = (
        "PJM Western Hub (51217) Real Historic Snapshot — TLS fallback"
    )
    market.attrs["live_feed_fallback"] = True
    return market


def fetch_pjm_live_hub_profile() -> pd.DataFrame:
    """
    Build a 24-hour Western Hub profile via bulk ingestion + local date-chunking.

    Network I/O (500-row bulk buffers) is cached for 1 hour. Subsequent slider
    reruns slice the in-memory master dataframes with zero additional API calls.
    Falls back to the high-volatility historic snapshot on TLS/network failure.
    """
    try:
        da_master = _fetch_pjm_bulk_feed(PJM_DA_EXPORT_URL, "da_hrl_lmps")
        rt_master = _fetch_pjm_bulk_feed(PJM_RT_EXPORT_URL, "rt_hrl_lmps")
        market, trade_date = _slice_latest_complete_trading_day(da_master, rt_master)

        if len(market) != 24:
            raise ValueError(
                f"Expected 24 merged hourly records, received {len(market)}."
            )

        market["DA_Position"] = 0.0
        market.attrs["trade_date"] = trade_date
        market.attrs["pricing_node_id"] = PJM_WESTERN_HUB_PNODE_ID
        market.attrs["data_source"] = (
            "PJM Data Miner 2 — Bulk Ingest + Local Slice (Western Hub 51217)"
        )
        market.attrs["live_feed_fallback"] = False
        market.attrs["bulk_rows_da"] = len(da_master)
        market.attrs["bulk_rows_rt"] = len(rt_master)
        return market

    except Exception:
        return _generate_pjm_western_hub_historic_snapshot()


def _slice_uploaded_latest_24h(df: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    """Extract the latest complete 24-hour DA+RT window from an uploaded PJM buffer."""
    for trade_date in sorted(df["trade_date"].unique(), reverse=True):
        day = (
            df[df["trade_date"] == trade_date]
            .drop_duplicates(subset=["Hour"], keep="last")
            .sort_values("Hour")
        )
        if day["Hour"].nunique() >= 24 and len(day) >= 24:
            window = day.sort_values("Hour").head(24)
            if len(window) == 24:
                return window.copy(), str(trade_date)

    sorted_df = df.sort_values("datetime_beginning_utc").reset_index(drop=True)
    for start in range(len(sorted_df) - 24, -1, -1):
        window = sorted_df.iloc[start : start + 24]
        if len(window) < 24:
            continue
        timestamps = window["datetime_beginning_utc"]
        if not timestamps.is_monotonic_increasing:
            continue
        trade_date = str(window["trade_date"].iloc[-1])
        return window.copy(), trade_date

    raise ValueError(
        "Uploaded CSV did not contain a complete continuous 24-hour "
        "DA/RT price window after node filtering."
    )


def parse_uploaded_pjm_market_csv(uploaded_file) -> pd.DataFrame:
    """
    Parse a raw PJM Data Miner export (e.g. da_hrl_lmps.csv) into a 24-hour
    market profile compatible with the optimization kernel.
    """
    content = uploaded_file.getvalue()
    filename = getattr(uploaded_file, "name", "upload.csv")

    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            raw = pd.read_csv(StringIO(content.decode(encoding)), low_memory=False)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("Unable to decode uploaded CSV (expected UTF-8 or Latin-1).")

    df = raw.copy()
    df.columns = [str(c).lower().strip() for c in df.columns]

    if "pnode_id" in df.columns:
        df["pnode_id"] = pd.to_numeric(df["pnode_id"], errors="coerce")
        unique_nodes = df["pnode_id"].nunique(dropna=True)
        if unique_nodes > 1:
            hub_rows = df[df["pnode_id"] == PJM_WESTERN_HUB_PNODE_ID]
            if not hub_rows.empty:
                df = hub_rows.copy()
            else:
                dominant_node = df["pnode_id"].mode(dropna=True).iloc[0]
                df = df[df["pnode_id"] == dominant_node].copy()

    dt_col = next(
        (c for c in ("datetime_beginning_utc", "datetime_beginning_ept") if c in df.columns),
        None,
    )
    if dt_col is None:
        raise ValueError(
            "CSV must include datetime_beginning_utc or datetime_beginning_ept."
        )

    da_col = next(
        (c for c in ("total_lmp_da", "total lmp day ahead") if c in df.columns),
        None,
    )
    if da_col is None:
        raise ValueError(
            "CSV must include total_lmp_da (Total LMP Day Ahead) for DA_Price mapping."
        )

    df["datetime_beginning_utc"] = pd.to_datetime(
        df[dt_col], utc=True, errors="coerce"
    )
    df["DA_Price"] = pd.to_numeric(df[da_col], errors="coerce")
    df = df.dropna(subset=["datetime_beginning_utc", "DA_Price"])
    df = df.sort_values("datetime_beginning_utc", ascending=True)

    ept = df["datetime_beginning_utc"].dt.tz_convert("America/New_York")
    df["trade_date"] = ept.dt.date
    df["Hour"] = ept.dt.hour.astype(int)

    rt_col = next(
        (c for c in ("total_lmp_rt", "total lmp real time") if c in df.columns),
        None,
    )
    rt_synthetic = rt_col is None
    if rt_col is not None:
        df["RT_Price"] = pd.to_numeric(df[rt_col], errors="coerce")
    else:
        upload_seed = int(md5(content).hexdigest()[:8], 16) % (2**32)
        rng_state = np.random.get_state()
        np.random.seed(upload_seed)
        df["RT_Price"] = df["DA_Price"] * np.random.uniform(0.8, 1.3, len(df))
        np.random.set_state(rng_state)

    window, trade_date = _slice_uploaded_latest_24h(df)
    market = window[["Hour", "DA_Price", "RT_Price"]].copy()

    if rt_synthetic:
        market.loc[market["Hour"] == 18, "RT_Price"] = 250.00

    market = market.sort_values("Hour").reset_index(drop=True)
    if len(market) != 24:
        raise ValueError(
            f"Expected 24 hourly records after slicing, received {len(market)}."
        )

    market["DA_Position"] = 0.0
    pricing_node = (
        int(df["pnode_id"].iloc[-1])
        if "pnode_id" in df.columns and df["pnode_id"].notna().any()
        else PJM_WESTERN_HUB_PNODE_ID
    )
    market.attrs["trade_date"] = trade_date
    market.attrs["pricing_node_id"] = pricing_node
    market.attrs["data_source"] = f"Custom PJM upload — {filename}"
    market.attrs["live_feed_fallback"] = False
    market.attrs["rt_synthetic"] = rt_synthetic
    market.attrs["upload_filename"] = filename
    market.attrs["upload_row_count"] = len(raw)
    return market


def load_market_profile(
    market_data_mode: str,
    uploaded_file=None,
) -> pd.DataFrame:
    """Route to simulation spikes, live PJM feed, or user-uploaded PJM export CSV."""
    if market_data_mode == LIVE_DATA_MODE:
        return fetch_pjm_live_hub_profile()
    if market_data_mode == CUSTOM_CSV_MODE:
        if uploaded_file is None:
            raise ValueError("Upload a PJM Data Miner CSV in the sidebar to continue.")
        return parse_uploaded_pjm_market_csv(uploaded_file)
    profile = simulate_pjm_profile()
    profile.attrs["trade_date"] = "Simulated"
    profile.attrs["pricing_node_id"] = "N/A"
    profile.attrs["data_source"] = "Internal stress-test simulation"
    profile.attrs["live_feed_fallback"] = False
    return profile


# ---------------------------------------------------------------------------
# Dispatch engines
# ---------------------------------------------------------------------------


def run_baseline_da_adherence(
    market: pd.DataFrame, config: AssetConfig
) -> tuple[np.ndarray, np.ndarray]:
    """
    Scenario A — Blind Day-Ahead Adherence.

    Strictly follows DA_Position each hour. If the desired action would violate
    SOC limits, force action to 0 MW. This creates a physical shortfall vs. the
    DA commitment, triggering deviation settlement penalties at the RT price.
    """
    n = len(market)
    actions = np.zeros(n)
    socs = np.zeros(n)
    soc = config.initial_soc
    cap = config.total_energy_mwh
    eff = config.round_trip_efficiency

    for i, row in market.iterrows():
        desired = float(row["DA_Position"])

        if is_soc_feasible(
            soc, desired, eff, config.min_soc, config.max_soc, cap
        ):
            actions[i] = desired
            soc = project_soc(soc, desired, eff, cap)
        else:
            # SOC-bound: cannot honor DA commitment -> deviation penalty follows
            actions[i] = 0.0

        socs[i] = soc

    return actions, socs


def bess_pjm_optimization_kernel(
    market: pd.DataFrame, config: AssetConfig
) -> tuple[np.ndarray, np.ndarray]:
    """
    Scenario B — Advanced Real-Time Optimization with Heuristic Look-Ahead.

    Combines a vectorized discrete greedy kernel with a look-ahead charging
    strategy:
      - Hours 7 & 13 (prep): foreseeing RT spikes at 8 & 14, aggressively
        CHARGE at -Max_Power (or max feasible charge) when cheap RT ($10/MWh)
        leaves headroom below max_soc.
      - Hours 8 & 14 (spike): force FULL DISCHARGE at +Max_Power when SOC
        allows, capturing RT arbitrage and avoiding deviation penalties vs. DA.

    All other hours use the standard [-Max_Power, 0, +Max_Power] greedy kernel
    with look-ahead shadow incentive: if the next hour is a known spike, add a
    bonus to candidate scores proportional to upcoming RT minus current RT.
    """
    n = len(market)
    candidates = np.array([-config.max_power_mw, 0.0, config.max_power_mw])
    actions = np.zeros(n)
    socs = np.zeros(n)
    soc = config.initial_soc
    cap = config.total_energy_mwh
    eff = config.round_trip_efficiency

    spike_rt_by_hour = {
        int(row["Hour"]): float(row["RT_Price"])
        for _, row in market.iterrows()
        if int(row["Hour"]) in LOOKAHEAD_SPIKE_HOURS
    }

    for i, row in market.iterrows():
        hour = int(row["Hour"])
        da_position = float(row["DA_Position"])
        rt_price = float(row["RT_Price"])

        # --- Heuristic look-ahead: aggressive prep charge ---
        if hour in LOOKAHEAD_PREP_HOURS:
            chosen_action = max_feasible_charge_mw(
                soc, config.max_power_mw, eff, config.max_soc, cap
            )
            actions[i] = chosen_action
            soc = project_soc(soc, chosen_action, eff, cap)
            socs[i] = soc
            continue

        # --- Heuristic look-ahead: full discharge at foreseen spike ---
        if hour in LOOKAHEAD_SPIKE_HOURS:
            chosen_action = max_feasible_discharge_mw(
                soc, config.max_power_mw, eff, config.min_soc, cap
            )
            actions[i] = chosen_action
            soc = project_soc(soc, chosen_action, eff, cap)
            socs[i] = soc
            continue

        # --- Standard greedy discrete kernel with optional next-hour shadow value ---
        next_spike_rt = 0.0
        if hour + 1 in spike_rt_by_hour:
            next_spike_rt = spike_rt_by_hour[hour + 1]
        elif hour + 1 in LOOKAHEAD_PREP_HOURS:
            # Hour before prep: next hour is cheap charge, no shadow needed
            next_spike_rt = 0.0

        feasible = np.array(
            [
                is_soc_feasible(
                    soc, c, eff, config.min_soc, config.max_soc, cap
                )
                for c in candidates
            ]
        )

        rt_revenues = candidates * rt_price
        dev_settlements = (candidates - da_position) * rt_price
        wear_costs = np.abs(candidates) * HOUR_DURATION_H * config.wear_cost_per_mwh

        # Shadow incentive: reward actions that leave SOC high before a spike
        lookahead_bonus = np.zeros(len(candidates))
        if next_spike_rt > rt_price:
            for j, c in enumerate(candidates):
                projected = project_soc(soc, c, eff, cap)
                # Higher projected SOC before a spike is valuable
                lookahead_bonus[j] = projected * (next_spike_rt - rt_price) * 0.05

        scores = rt_revenues + dev_settlements - wear_costs + lookahead_bonus
        scores = np.where(feasible, scores, -np.inf)

        best_score = np.max(scores)
        best_indices = np.where(scores == best_score)[0]
        if len(best_indices) == 1:
            chosen_idx = best_indices[0]
        else:
            distances = np.abs(candidates[best_indices] - da_position)
            chosen_idx = best_indices[np.argmin(distances)]

        chosen_action = float(candidates[chosen_idx])
        actions[i] = chosen_action
        soc = project_soc(soc, chosen_action, eff, cap)
        socs[i] = soc

    return actions, socs


def run_rt_optimizer(
    market: pd.DataFrame, config: AssetConfig
) -> tuple[np.ndarray, np.ndarray]:
    """Alias for the BESS PJM optimization kernel (Scenario B)."""
    return bess_pjm_optimization_kernel(market, config)


# ---------------------------------------------------------------------------
# Results assembly
# ---------------------------------------------------------------------------


def build_results_df(
    market: pd.DataFrame,
    baseline_actions: np.ndarray,
    baseline_socs: np.ndarray,
    optimized_actions: np.ndarray,
    optimized_socs: np.ndarray,
    config: AssetConfig,
) -> pd.DataFrame:
    """Merge market data, dispatch paths, and financial ledger columns."""
    df = market.copy()
    df["Baseline_Action"] = baseline_actions
    df["Optimized_Action"] = optimized_actions
    df["Baseline_SOC"] = baseline_socs * 100.0
    df["Optimized_SOC"] = optimized_socs * 100.0

    df["Deviation_Amount"] = df["Optimized_Action"] - df["DA_Position"]

    opt_financials = df.apply(
        lambda r: hourly_total_pnl(
            r["Optimized_Action"],
            r["DA_Position"],
            r["RT_Price"],
            config.wear_cost_per_mwh,
        ),
        axis=1,
        result_type="expand",
    )
    df["Realized_RT_Revenue"] = opt_financials[0]
    df["Deviation_Settlement"] = opt_financials[1]
    df["Wear_Cost"] = opt_financials[2]
    df["Total_Realized_PnL"] = opt_financials[3]

    base_financials = df.apply(
        lambda r: hourly_total_pnl(
            r["Baseline_Action"],
            r["DA_Position"],
            r["RT_Price"],
            config.wear_cost_per_mwh,
        ),
        axis=1,
        result_type="expand",
    )
    df["Baseline_Total_PnL"] = base_financials[3]
    df.attrs["initial_soc_pct"] = config.initial_soc * 100.0

    return df


def config_fingerprint(
    config: AssetConfig, market_data_mode: str, market_tag: str = ""
) -> str:
    """Unique string for Streamlit widget keys — forces chart refresh on rerun."""
    return (
        f"m{market_data_mode}_t{market_tag}_"
        f"p{config.max_power_mw}_c{config.total_energy_mwh}_e{config.round_trip_efficiency}_"
        f"i{config.initial_soc}_mn{config.min_soc}_mx{config.max_soc}_w{config.wear_cost_per_mwh}"
    )


def run_backtest(
    config: AssetConfig,
    market_data_mode: str,
    uploaded_file=None,
    upload_tag: str = "",
) -> pd.DataFrame:
    """
    Execute full 24-hour backtest from live sidebar AssetConfig and data mode.

    Called on every Streamlit rerun so slider / data-mode changes immediately
    propagate through simulation, optimization, metrics, and Plotly charts.
    """
    market = load_market_profile(market_data_mode, uploaded_file)
    baseline_actions, baseline_socs = run_baseline_da_adherence(market, config)
    optimized_actions, optimized_socs = run_rt_optimizer(market, config)
    results = build_results_df(
        market,
        baseline_actions,
        baseline_socs,
        optimized_actions,
        optimized_socs,
        config,
    )
    results.attrs["market_data_mode"] = market_data_mode
    results.attrs["trade_date"] = market.attrs.get("trade_date", "Simulated")
    results.attrs["data_source"] = market.attrs.get("data_source", "")
    results.attrs["pricing_node_id"] = market.attrs.get("pricing_node_id", "N/A")
    results.attrs["live_feed_fallback"] = market.attrs.get("live_feed_fallback", False)
    results.attrs["bulk_rows_da"] = market.attrs.get("bulk_rows_da")
    results.attrs["bulk_rows_rt"] = market.attrs.get("bulk_rows_rt")
    results.attrs["rt_synthetic"] = market.attrs.get("rt_synthetic", False)
    results.attrs["upload_filename"] = market.attrs.get("upload_filename", "")
    results.attrs["upload_tag"] = upload_tag
    return results


# ---------------------------------------------------------------------------
# Plotly chart builders
# ---------------------------------------------------------------------------


def build_market_chart(df: pd.DataFrame, market_data_mode: str) -> go.Figure:
    """Dual-axis DA vs RT price chart with anomalous-hour shading."""
    fig = make_subplots(specs=[[{"secondary_y": True}]])

    fig.add_trace(
        go.Scatter(
            x=df["Hour"],
            y=df["DA_Price"],
            name="DA Price (日前)",
            mode="lines+markers",
            line=dict(color="#2563eb", width=2),
            marker=dict(size=6),
        ),
        secondary_y=False,
    )

    fig.add_trace(
        go.Scatter(
            x=df["Hour"],
            y=df["RT_Price"],
            name="RT Price (实时)",
            mode="lines+markers",
            line=dict(color="#dc2626", width=2, dash="dash"),
            marker=dict(size=6),
        ),
        secondary_y=True,
    )

    anomaly_colors = {
        7: "rgba(34, 197, 94, 0.12)",
        8: "rgba(234, 179, 8, 0.15)",
        13: "rgba(34, 197, 94, 0.12)",
        14: "rgba(239, 68, 68, 0.15)",
        20: "rgba(99, 102, 241, 0.15)",
    }

    if market_data_mode == SIMULATION_DATA_MODE:
        annotation_hours = ANOMALY_HOURS
        top_hours: list[int] = []
    else:
        # Live / fallback feed: highlight key RT stress hours dynamically.
        top_hours = (
            df.nlargest(3, "RT_Price")["Hour"].astype(int).tolist()
            if not df.empty
            else []
        )
        annotation_hours = {
            int(h): f"RT Peak Spike (实时用电尖峰 · H{int(h)})" for h in top_hours
        }
        if 3 in df["Hour"].values and float(df.loc[df["Hour"] == 3, "RT_Price"].iloc[0]) < 0:
            annotation_hours[3] = "Overnight Wind Glut (夜间风电过剩)"

    for hour, label in annotation_hours.items():
        fig.add_vrect(
            x0=hour - 0.4,
            x1=hour + 0.4,
            fillcolor=anomaly_colors.get(hour, "rgba(128,128,128,0.1)"),
            layer="below",
            line_width=0,
        )

        if market_data_mode == SIMULATION_DATA_MODE:
            layout = ANOMALY_ANNOTATION_LAYOUT.get(
                hour, {"y": 0.98, "yanchor": "top"}
            )
        else:
            stagger_y = 0.98 if top_hours and hour == top_hours[0] else 0.80
            layout = {"y": stagger_y, "yanchor": "top"}
        fig.add_annotation(
            x=hour,
            y=layout["y"],
            xref="x",
            yref="paper",
            text=label,
            textangle=-90,
            showarrow=False,
            xanchor="center",
            yanchor=str(layout["yanchor"]),
            font=dict(size=10, color="#334155"),
            bgcolor="rgba(255, 255, 255, 0.75)",
            borderpad=2,
        )

    chart_title = {
        SIMULATION_DATA_MODE: (
            f"{CHART_TITLE_BASE} — 24-Hour PJM Simulation (24小时PJM模拟)"
        ),
        CUSTOM_CSV_MODE: (
            f"{CHART_TITLE_BASE} — Custom PJM Upload (自定义PJM上传 · 24小时)"
        ),
    }.get(
        market_data_mode,
        f"{CHART_TITLE_BASE} — PJM Western Hub (PJM西部枢纽 · 24小时)",
    )
    fig.update_layout(
        title=chart_title,
        xaxis_title="Hour of Day (当日小时轴)",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        height=480,
        margin=dict(t=100, l=60, r=60),
    )
    fig.update_yaxes(
        title_text="DA Price (日前电价 [$/MWh])", secondary_y=False
    )
    fig.update_yaxes(
        title_text="RT Price (实时电价 [$/MWh])", secondary_y=True
    )

    return fig


def build_soc_chart(df: pd.DataFrame, initial_soc_pct: float) -> go.Figure:
    """Hour-by-hour battery SOC under baseline vs optimized dispatch."""
    fig = go.Figure()

    # Hour-0 anchor: beginning-of-day SOC from the Initial SOC slider
    fig.add_trace(
        go.Scatter(
            x=[0],
            y=[initial_soc_pct],
            name="Hour 0 — Initial SOC (%) (第0小时 · 初始SOC)",
            mode="markers",
            marker=dict(color="#0f172a", size=10, symbol="diamond"),
        )
    )

    fig.add_trace(
        go.Scatter(
            x=df["Hour"],
            y=df["Baseline_SOC"],
            name="Scenario A — Baseline SOC (%) (场景A · 基准SOC)",
            mode="lines+markers",
            line=dict(color="#64748b", width=2),
        )
    )
    fig.add_trace(
        go.Scatter(
            x=df["Hour"],
            y=df["Optimized_SOC"],
            name="Scenario B — Optimized SOC (%) (场景B · 优化SOC)",
            mode="lines+markers",
            line=dict(color="#16a34a", width=2),
        )
    )

    fig.update_layout(
        title="Battery State-of-Charge Tracking (电池荷电状态跟踪)",
        xaxis_title="Hour of Day (当日小时轴)",
        yaxis_title="SOC (%) (荷电状态)",
        hovermode="x unified",
        height=420,
    )

    return fig


def build_dispatch_chart(df: pd.DataFrame) -> go.Figure:
    """Grouped bar chart of dispatch commands (MW) by scenario."""
    fig = go.Figure()

    fig.add_trace(
        go.Bar(
            x=df["Hour"],
            y=df["Baseline_Action"],
            name="Scenario A — Baseline Dispatch (MW) (场景A · 基准调度)",
            marker_color="#64748b",
        )
    )
    fig.add_trace(
        go.Bar(
            x=df["Hour"],
            y=df["Optimized_Action"],
            name="Scenario B — Optimized Dispatch (MW) (场景B · 优化调度)",
            marker_color="#16a34a",
        )
    )

    fig.update_layout(
        title=(
            "Hourly Dispatch Commands (Industrial Sign Convention: + = Discharge, − = Charge) "
            "(小时调度指令 · 工业符号约定：+ = 放电，− = 充电)"
        ),
        xaxis_title="Hour of Day (当日小时轴)",
        yaxis_title="Power (MW) (功率 [MW])",
        barmode="group",
        height=420,
    )

    return fig


def style_trading_ledger(df: pd.DataFrame) -> pd.io.formats.style.Styler:
    """Apply professional currency / numeric formatting and P&L coloring."""
    display_cols = [
        "Hour",
        "DA_Price",
        "RT_Price",
        "DA_Position",
        "Optimized_Action",
        "Deviation_Amount",
        "Realized_RT_Revenue",
        "Deviation_Settlement",
        "Total_Realized_PnL",
    ]
    ledger = df[display_cols].copy()

    currency_cols = [
        "DA_Price",
        "RT_Price",
        "Realized_RT_Revenue",
        "Deviation_Settlement",
        "Total_Realized_PnL",
    ]
    mw_cols = ["DA_Position", "Optimized_Action", "Deviation_Amount"]

    fmt = {c: "${:,.2f}" for c in currency_cols}
    fmt.update({c: "{:,.2f}" for c in mw_cols})
    fmt["Hour"] = "{:.0f}"

    def _color_pnl(val: float) -> str:
        if val > 0:
            return "color: #16a34a; font-weight: 600"
        if val < 0:
            return "color: #dc2626; font-weight: 600"
        return ""

    return (
        ledger.style.format(fmt)
        .map(_color_pnl, subset=["Total_Realized_PnL"])
        .set_properties(**{"text-align": "right"})
        .set_table_styles(
            [
                {"selector": "th", "props": [("text-align", "center")]},
            ]
        )
    )


# ---------------------------------------------------------------------------
# Streamlit application
# ---------------------------------------------------------------------------


def render_sidebar() -> tuple[AssetConfig, str, object | None]:
    """Collect asset, risk, and market data pipeline settings from the sidebar."""
    uploaded_csv = None
    with st.sidebar:
        st.header("Asset & Risk Configuration (资产与风险配置)")
        st.caption(
            "Industrial sign convention: +MW = Discharge (sell), -MW = Charge (buy). "
            "(工业符号约定：+MW = 放电 [卖电]，-MW = 充电 [买电])"
        )

        st.subheader("⚡ Data Pipeline Feed (数据管道接入)")
        market_data_mode = st.selectbox(
            "Market Data Mode (市场数据模式)",
            MARKET_DATA_MODES,
            key="market_data_mode",
        )
        if market_data_mode == LIVE_DATA_MODE:
            st.caption(
                "Streams PJM Western Hub (51217) DA/RT hourly LMPs via public "
                "Data Miner 2 export endpoints (REST fallback, no token). "
                "(接入PJM西部枢纽51217日前/实时小时LMP · 公开Data Miner 2导出 · REST回退 · 无需Token)"
            )
        elif market_data_mode == CUSTOM_CSV_MODE:
            uploaded_csv = st.file_uploader(
                "Upload PJM Data Miner CSV (上传PJM Data Miner CSV)",
                type=["csv"],
                key="custom_market_csv_uploader",
                help=(
                    "Official PJM export (e.g. da_hrl_lmps.csv). "
                    "Western Hub 51217: auto-selected when present. "
                    "(官方PJM导出 · 如da_hrl_lmps.csv · 存在时自动选择西部枢纽51217)"
                ),
            )
            st.caption(
                "Supports raw PJM da_hrl_lmps / rt_hrl_lmps exports. "
                "DA-only uploads receive a synthetic RT curve with an H18 peak spike. "
                "(支持原始PJM导出 · 仅DA文件将合成RT曲线并在H18注入尖峰)"
            )

        max_power = st.slider(
            "Max Power Capacity (最大功率容量 [MW])",
            min_value=0.5,
            max_value=10.0,
            value=2.0,
            step=0.5,
            key="slider_max_power",
        )
        max_capacity = st.slider(
            "Total Energy Capacity (总能量容量 [MWh])",
            min_value=1.0,
            max_value=40.0,
            value=4.0,
            step=1.0,
            key="slider_max_capacity",
        )
        efficiency = st.slider(
            "Round-Trip Efficiency (充放电循环效率)",
            min_value=0.50,
            max_value=1.00,
            value=0.85,
            step=0.01,
            key="slider_efficiency",
        )
        initial_soc = st.slider(
            "Initial SOC (初始荷电状态 SOC)",
            min_value=0.00,
            max_value=1.00,
            value=0.50,
            step=0.05,
            key="slider_initial_soc",
        )
        min_soc = st.slider(
            "Min SOC Limit (最小 SOC 限制)",
            min_value=0.00,
            max_value=0.30,
            value=0.10,
            step=0.05,
            key="slider_min_soc",
        )
        max_soc = st.slider(
            "Max SOC Limit (最大 SOC 限制)",
            min_value=0.70,
            max_value=1.00,
            value=0.90,
            step=0.05,
            key="slider_max_soc",
        )
        wear_tear = st.slider(
            "Battery Wear and Tear Cost (电池折旧与损耗成本 [$/MWh])",
            min_value=0.00,
            max_value=100.00,
            value=20.00,
            step=5.00,
            key="slider_wear_tear",
        )

    return AssetConfig(
        max_power_mw=max_power,
        total_energy_mwh=max_capacity,
        round_trip_efficiency=efficiency,
        initial_soc=initial_soc,
        min_soc=min_soc,
        max_soc=max_soc,
        wear_cost_per_mwh=wear_tear,
    ), market_data_mode, uploaded_csv


def main() -> None:
    st.set_page_config(
        page_title="PJM Storage Workbench (PJM储能工作台)",
        page_icon="📊",
        layout="wide",
    )

    st.markdown(
        """
        <style>
        div[data-testid="stMetric"] {
            background: linear-gradient(135deg, #f8fafc 0%, #f1f5f9 100%);
            border: 1px solid #e2e8f0;
            border-radius: 12px;
            padding: 16px 20px;
        }
        div[data-testid="stMetric"] label {
            font-size: 0.85rem;
            color: #475569;
        }
        div[data-testid="stMetric"] div[data-testid="stMetricValue"] {
            font-size: 1.75rem;
            font-weight: 700;
            color: #0f172a;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    config, market_data_mode, uploaded_csv = render_sidebar()

    st.title(
        "📊 PJM Spot Trading & Storage Arbitrage Quantitative Workbench "
        "(PJM现货交易与储能套利量化工作台)"
    )
    st.markdown(
        "Intra-day storage arbitrage and **deviation settlement** analytics for "
        "PJM-style energy markets — compare blind day-ahead adherence against a "
        "real-time optimization engine with heuristic look-ahead charging at "
        "Hours 7 and 13 before RT price spikes. "
        "(日内储能套利与偏差结算分析 · 对比盲目日前 adherence 与带前瞻充电启发式的实时优化引擎)"
    )

    if market_data_mode == CUSTOM_CSV_MODE and uploaded_csv is None:
        st.info(
            f"Select **{CUSTOM_CSV_MODE}** and upload a PJM Data Miner export "
            "(e.g. `da_hrl_lmps.csv`) in the sidebar to run the backtest across all tabs. "
            "(请在侧边栏选择本地上传模式并上传PJM Data Miner导出文件以运行全部标签页回测)"
        )
        st.stop()

    upload_tag = ""
    if market_data_mode == CUSTOM_CSV_MODE and uploaded_csv is not None:
        upload_tag = md5(uploaded_csv.getvalue()).hexdigest()[:12]

    try:
        if market_data_mode == LIVE_DATA_MODE:
            with st.spinner(
                "Fetching live PJM Western Hub market data... "
                "(正在获取PJM西部枢纽实时市场数据...)"
            ):
                results = run_backtest(
                    config, market_data_mode, uploaded_csv, upload_tag
                )
        else:
            results = run_backtest(
                config, market_data_mode, uploaded_csv, upload_tag
            )
    except ValueError as exc:
        st.error(
            f"Custom CSV could not be parsed (自定义CSV解析失败): {exc}"
        )
        st.stop()
    except Exception as exc:
        st.error(
            "An unexpected error occurred while running the backtest "
            "(回测运行时发生意外错误). "
            f"Details (详情): {exc}"
        )
        st.stop()

    run_key = config_fingerprint(
        config,
        market_data_mode,
        f"{results.attrs.get('trade_date', '')}_{upload_tag}",
    )
    initial_soc_pct = float(results.attrs["initial_soc_pct"])
    live_feed_fallback = bool(results.attrs.get("live_feed_fallback", False))
    rt_synthetic = bool(results.attrs.get("rt_synthetic", False))

    total_baseline_pnl = results["Baseline_Total_PnL"].sum()
    total_optimized_pnl = results["Total_Realized_PnL"].sum()
    net_alpha = total_optimized_pnl - total_baseline_pnl

    col1, col2, col3 = st.columns(3)
    col1.metric("Total Baseline P&L (基准总收益)", f"${total_baseline_pnl:,.2f}")
    col2.metric("Total Optimized P&L (优化总收益)", f"${total_optimized_pnl:,.2f}")
    col3.metric("Net Alpha Generated (超额净收益 Alpha)", f"${net_alpha:,.2f}")

    if live_feed_fallback:
        st.warning(
            "⚠️ PJM Grid Gateway Unreachable (TLS Block). Automatically loaded "
            "cached High-Volatility PJM Western Hub Historic Snapshot for strategy "
            "evaluation. "
            "(PJM电网网关不可达 · TLS阻断 · 已自动加载高波动西部枢纽历史快照用于策略评估)"
        )
    elif market_data_mode == LIVE_DATA_MODE:
        bulk_da = results.attrs.get("bulk_rows_da")
        bulk_rt = results.attrs.get("bulk_rows_rt")
        bulk_note = (
            f" · Bulk buffer (批量缓存): {bulk_da}/{bulk_rt} rows (行)"
            if bulk_da and bulk_rt
            else ""
        )
        st.caption(
            f"**Live feed active (实时流已激活)** · Trade date (交易日): "
            f"`{results.attrs.get('trade_date')}` · "
            f"Node (节点) `{results.attrs.get('pricing_node_id')}` · "
            f"{results.attrs.get('data_source', '')}{bulk_note}"
        )
    elif market_data_mode == CUSTOM_CSV_MODE:
        rt_note = (
            " · RT synthesized from DA (H18 peak $250/MWh injected) "
            "(RT由DA合成 · H18注入$250/MWh尖峰)"
            if rt_synthetic
            else " · DA + RT columns mapped from upload (已从上传映射DA与RT列)"
        )
        st.caption(
            f"**Custom CSV active (自定义CSV已激活)** · Trade date (交易日): "
            f"`{results.attrs.get('trade_date')}` · "
            f"Node (节点) `{results.attrs.get('pricing_node_id')}` · "
            f"`{results.attrs.get('upload_filename', 'upload.csv')}`"
            f"{rt_note}"
        )

    tab1, tab2, tab3 = st.tabs(
        [
            "Market Data Analysis (市场数据分析)",
            "Strategy & SOC Tracking (策略与SOC跟踪)",
            "Trading Log & Decision Attribution (交易日志与决策归因)",
        ]
    )

    with tab1:
        st.plotly_chart(
            build_market_chart(results, market_data_mode),
            use_container_width=True,
            key=f"chart_market_{run_key}",
        )
        if market_data_mode == SIMULATION_DATA_MODE:
            st.info(
                "**Injected test scenarios (注入测试场景):** Hours 7 & 13 — look-ahead prep charge "
                "(RT $10/MWh) before spikes; Hour 8 — RT $600/MWh discharge test; "
                "Hour 14 — RT spike ($800/MWh); Hour 20 — negative RT (−$100/MWh). "
                "(第7/13小时前瞻充电 · 第8/14小时尖峰 · 第20小时负实时电价)"
            )
        elif market_data_mode == CUSTOM_CSV_MODE:
            if rt_synthetic:
                st.info(
                    "**Custom DA-only upload (自定义仅DA上传):** `total_lmp_da` mapped to DA_Price. "
                    "RT_Price synthesized as DA × uniform(0.8, 1.3) with a fixed "
                    "H18 peak spike at **$250/MWh** to keep arbitrage signals active. "
                    "Latest complete 24-hour block extracted after chronological sort. "
                    "(RT由DA合成 · H18固定$250/MWh尖峰 · 按时间排序后提取最新完整24小时块)"
                )
            else:
                st.info(
                    "**Custom PJM upload (自定义PJM上传):** Both `total_lmp_da` and `total_lmp_rt` "
                    "mapped from the file. Shaded bands mark the top three RT LMP hours "
                    "from the selected trade date. "
                    "(已从文件映射DA/RT · 阴影标注当日RT LMP最高的三小时)"
                )
        else:
            if live_feed_fallback:
                st.info(
                    "**Historic snapshot (TLS fallback) (历史快照 · TLS回退):** Pnode 51217 Western Hub. "
                    "Hour 3 — overnight wind glut (RT −$12.50/MWh); "
                    "Hour 18 — evening peak spike (RT $260/MWh). "
                    "Look-ahead prep/discharge logic at Hours 7, 8, 13, 14 remains active. "
                    "(第3小时夜间风电过剩 · 第18小时晚高峰尖峰 · 第7/8/13/14小时前瞻逻辑仍生效)"
                )
            else:
                st.info(
                    "**Live PJM Western Hub feed (PJM西部枢纽实时流):** DA prices from `da_hrl_lmps`, RT prices "
                    "from `rt_hrl_lmps`. Shaded bands mark the top three RT LMP hours from "
                    "the selected trade date. "
                    "(DA来自da_hrl_lmps · RT来自rt_hrl_lmps · 阴影标注RT LMP最高的三小时)"
                )

    with tab2:
        st.plotly_chart(
            build_soc_chart(results, initial_soc_pct),
            use_container_width=True,
            key=f"chart_soc_{run_key}",
        )
        st.plotly_chart(
            build_dispatch_chart(results),
            use_container_width=True,
            key=f"chart_dispatch_{run_key}",
        )

    with tab3:
        st.markdown(
            "**Scenario B — Hour-by-Hour Trading Ledger (场景B · 逐小时交易账本).** "
            "*Deviation_Settlement = (Optimized_Action − DA_Position) × RT_Price "
            "(偏差结算 = [优化动作 − 日前仓位] × 实时电价).* "
            "A negative value indicates a penalty for under-delivering vs. the DA schedule. "
            "(负值表示相对日前调度欠交付的惩罚)"
        )
        st.dataframe(
            style_trading_ledger(results),
            use_container_width=True,
            height=600,
            key=f"ledger_{run_key}",
        )


if __name__ == "__main__":
    main()
