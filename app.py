"""
PJM Intra-day Storage Arbitrage & Deviation Settlement Analytics Workbench

Industrial Sign Convention (enforced throughout):
  - DISCHARGE (selling power)  -> POSITIVE MW  (> 0)
  - CHARGE    (buying power)   -> NEGATIVE MW  (< 0)
  - Deviation Settlement       -> (Actual_Action - DA_Position) * RT_Price
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HOUR_DURATION_H = 0.25  # SOC kernel time step (hours) per dispatch interval

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
    7: "Look-Ahead Charge (pre-H8)",
    8: "Low SOC / RT Spike Test",
    13: "Look-Ahead Charge (pre-H14)",
    14: "RT Spike ($800/MWh)",
    20: "Negative RT Price",
}

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


def config_fingerprint(config: AssetConfig) -> str:
    """Unique string for Streamlit widget keys — forces chart refresh on slider change."""
    return (
        f"p{config.max_power_mw}_c{config.total_energy_mwh}_e{config.round_trip_efficiency}_"
        f"i{config.initial_soc}_mn{config.min_soc}_mx{config.max_soc}_w{config.wear_cost_per_mwh}"
    )


def run_backtest(config: AssetConfig) -> pd.DataFrame:
    """
    Execute full 24-hour backtest from live sidebar AssetConfig.

    Called on every Streamlit rerun so slider changes immediately propagate
    through simulation, optimization, metrics, and Plotly charts.
    """
    market = simulate_pjm_profile()
    baseline_actions, baseline_socs = run_baseline_da_adherence(market, config)
    optimized_actions, optimized_socs = run_rt_optimizer(market, config)
    return build_results_df(
        market,
        baseline_actions,
        baseline_socs,
        optimized_actions,
        optimized_socs,
        config,
    )


# ---------------------------------------------------------------------------
# Plotly chart builders
# ---------------------------------------------------------------------------


def build_market_chart(df: pd.DataFrame) -> go.Figure:
    """Dual-axis DA vs RT price chart with anomalous-hour shading."""
    fig = make_subplots(specs=[[{"secondary_y": True}]])

    fig.add_trace(
        go.Scatter(
            x=df["Hour"],
            y=df["DA_Price"],
            name="DA Price ($/MWh)",
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
            name="RT Price ($/MWh)",
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
    for hour, label in ANOMALY_HOURS.items():
        fig.add_vrect(
            x0=hour - 0.4,
            x1=hour + 0.4,
            fillcolor=anomaly_colors.get(hour, "rgba(128,128,128,0.1)"),
            layer="below",
            line_width=0,
        )

        layout = ANOMALY_ANNOTATION_LAYOUT.get(
            hour, {"y": 0.98, "yanchor": "top"}
        )
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

    fig.update_layout(
        title="Day-Ahead vs Real-Time Price Profile (24-Hour PJM Simulation)",
        xaxis_title="Hour of Day",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        height=480,
        margin=dict(t=100, l=60, r=60),
    )
    fig.update_yaxes(title_text="DA Price ($/MWh)", secondary_y=False)
    fig.update_yaxes(title_text="RT Price ($/MWh)", secondary_y=True)

    return fig


def build_soc_chart(df: pd.DataFrame, initial_soc_pct: float) -> go.Figure:
    """Hour-by-hour battery SOC under baseline vs optimized dispatch."""
    fig = go.Figure()

    # Hour-0 anchor: beginning-of-day SOC from the Initial SOC slider
    fig.add_trace(
        go.Scatter(
            x=[0],
            y=[initial_soc_pct],
            name="Hour 0 — Initial SOC (%)",
            mode="markers",
            marker=dict(color="#0f172a", size=10, symbol="diamond"),
        )
    )

    fig.add_trace(
        go.Scatter(
            x=df["Hour"],
            y=df["Baseline_SOC"],
            name="Scenario A — Baseline SOC (%)",
            mode="lines+markers",
            line=dict(color="#64748b", width=2),
        )
    )
    fig.add_trace(
        go.Scatter(
            x=df["Hour"],
            y=df["Optimized_SOC"],
            name="Scenario B — Optimized SOC (%)",
            mode="lines+markers",
            line=dict(color="#16a34a", width=2),
        )
    )

    fig.update_layout(
        title="Battery State-of-Charge Tracking",
        xaxis_title="Hour of Day",
        yaxis_title="SOC (%)",
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
            name="Scenario A — Baseline Dispatch (MW)",
            marker_color="#64748b",
        )
    )
    fig.add_trace(
        go.Bar(
            x=df["Hour"],
            y=df["Optimized_Action"],
            name="Scenario B — Optimized Dispatch (MW)",
            marker_color="#16a34a",
        )
    )

    fig.update_layout(
        title="Hourly Dispatch Commands (Industrial Sign Convention: + = Discharge, − = Charge)",
        xaxis_title="Hour of Day",
        yaxis_title="Power (MW)",
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


def render_sidebar() -> AssetConfig:
    """Collect asset and risk parameters from the sidebar."""
    with st.sidebar:
        st.header("Asset & Risk Configuration")
        st.caption(
            "Industrial sign convention: +MW = Discharge (sell), -MW = Charge (buy)."
        )

        max_power = st.slider(
            "Max Power Capacity (MW)",
            min_value=0.5,
            max_value=10.0,
            value=2.0,
            step=0.5,
            key="slider_max_power",
        )
        max_capacity = st.slider(
            "Total Energy Capacity (MWh)",
            min_value=1.0,
            max_value=40.0,
            value=4.0,
            step=1.0,
            key="slider_max_capacity",
        )
        efficiency = st.slider(
            "Round-Trip Efficiency",
            min_value=0.50,
            max_value=1.00,
            value=0.85,
            step=0.01,
            key="slider_efficiency",
        )
        initial_soc = st.slider(
            "Initial SOC (fraction)",
            min_value=0.00,
            max_value=1.00,
            value=0.50,
            step=0.05,
            key="slider_initial_soc",
        )
        min_soc = st.slider(
            "Min SOC Limit",
            min_value=0.00,
            max_value=0.30,
            value=0.10,
            step=0.05,
            key="slider_min_soc",
        )
        max_soc = st.slider(
            "Max SOC Limit",
            min_value=0.70,
            max_value=1.00,
            value=0.90,
            step=0.05,
            key="slider_max_soc",
        )
        wear_tear = st.slider(
            "Battery Wear-and-Tear Cost ($/MWh)",
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
    )


def main() -> None:
    st.set_page_config(
        page_title="PJM Storage Workbench",
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

    config = render_sidebar()
    run_key = config_fingerprint(config)

    st.title("📊 PJM Spot Trading & Storage Arbitrage Quantitative Workbench")
    st.markdown(
        "Intra-day storage arbitrage and **deviation settlement** analytics for "
        "PJM-style energy markets. Compare blind day-ahead adherence against a "
        "real-time optimization engine with **heuristic look-ahead charging** "
        "at Hours 7 and 13 before RT price spikes."
    )

    results = run_backtest(config)
    initial_soc_pct = float(results.attrs["initial_soc_pct"])

    total_baseline_pnl = results["Baseline_Total_PnL"].sum()
    total_optimized_pnl = results["Total_Realized_PnL"].sum()
    net_alpha = total_optimized_pnl - total_baseline_pnl

    col1, col2, col3 = st.columns(3)
    col1.metric("Total Baseline P&L ($)", f"${total_baseline_pnl:,.2f}")
    col2.metric("Total Optimized P&L ($)", f"${total_optimized_pnl:,.2f}")
    col3.metric("Net Alpha Generated ($)", f"${net_alpha:,.2f}")

    tab1, tab2, tab3 = st.tabs(
        [
            "Market Data Analysis",
            "Strategy & SOC Tracking",
            "Trading Log & Decision Attribution",
        ]
    )

    with tab1:
        st.plotly_chart(
            build_market_chart(results),
            use_container_width=True,
            key=f"chart_market_{run_key}",
        )
        st.info(
            "**Injected test scenarios:** Hours 7 & 13 — look-ahead prep charge "
            "(RT $10/MWh) before spikes; Hour 8 — RT $600/MWh discharge test; "
            "Hour 14 — RT spike ($800/MWh); Hour 20 — negative RT (−$100/MWh)."
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
            "**Scenario B — Hour-by-Hour Trading Ledger.** "
            "*Deviation_Settlement = (Optimized_Action − DA_Position) × RT_Price.* "
            "A negative value indicates a penalty for under-delivering vs. the DA schedule."
        )
        st.dataframe(
            style_trading_ledger(results),
            use_container_width=True,
            height=600,
            key=f"ledger_{run_key}",
        )


if __name__ == "__main__":
    main()
