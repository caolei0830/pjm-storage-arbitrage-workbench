# PJM Spot Trading & Storage Arbitrage Quantitative Workbench

> **A production-grade interactive analytics platform for battery storage operators, quantitative researchers, and energy trading desks operating under PJM-style dual-settlement market rules.**

---

## Executive Summary

The **PJM Spot Trading & Storage Arbitrage Quantitative Workbench** is an end-to-end decision-support environment designed to quantify the economic value—and risk—of intra-day battery dispatch under a **dual-settlement** market structure. Participants commit to a **Day-Ahead (DA)** schedule, then settle **real-time (RT) deviations** against Locational Marginal Prices (LMPs) as physical operations unfold.

This platform simulates a 24-hour PJM price and position profile, runs two competing dispatch engines side-by-side, and surfaces the resulting P&L attribution through an institutional-grade Streamlit dashboard. Its core objective is to answer a question every storage quant must defend in an interview or risk committee:

**Does intelligent real-time optimization generate alpha—or does myopic dispatch, coupled with state-of-charge (SOC) binding constraints, destroy it through deviation penalties?**

Key capabilities include:

- **Dual-engine backtesting:** Scenario A (blind DA adherence) vs. Scenario B (discrete RT optimization with look-ahead heuristics)
- **Industrial sign convention enforcement:** `+MW = Discharge (Sell)` · `−MW = Charge (Buy)`
- **Deviation settlement modeling:** `(Actual − DA Position) × RT Price`
- **Reactive parametric stress testing** via sidebar sliders (power, energy, efficiency, SOC bounds, wear-and-tear cost)
- **Full P&L decomposition** across RT physical revenue, deviation settlement, and battery degradation cost

---

## 📈 The "Backtest Shock" & Alpha Capture Journey

This project evolved through three distinct phases—each exposing a different layer of storage trading risk. Together, they form a complete narrative arc from naive compliance to cross-temporal arbitrage.

---

### Phase A — Scenario A: Blind Day-Ahead Adherence (Baseline)

The baseline engine strictly follows the committed `DA_Position` each hour. When SOC limits prevent honoring the schedule, dispatch is forced to `0 MW`, triggering **deviation settlement penalties** at the RT price.

| Metric | Result |
|---|---|
| Strategy | Follow DA schedule; clamp to zero when SOC-infeasible |
| Key Risk | Physical default under real-time volatility |
| Outcome | Exposed to RT spikes without adaptive re-positioning |

**Lesson:** Compliance alone is not a strategy. A battery that cannot physically deliver its DA commitment becomes a **short option on RT prices**.

---

### Phase B — Scenario B: Myopic Greedy Failure (The Hour 14 Shock)

The first optimization engine applied a **myopic, hour-by-hour greedy kernel** over `{−P_max, 0, +P_max}` without foresight. During injected stress hours, the battery depleted SOC in earlier intervals and arrived at **Hour 14 (RT = $800/MWh)** with insufficient headroom above the **10% min SOC** boundary.

| Metric | Reference Backtest Result |
|---|---|
| Total Baseline P&L | **−$110.00** |
| Total Optimized P&L | **−$1,500.00** |
| **Net Alpha Generated** | **−$1,390.00** |
| Hour 14 Dispatch | `0.0 MW` (SOC-bound) |
| Hour 14 Deviation | `−1.0 MW` → **−$800 deviation loss** |

**Root Cause Diagnosis:**

1. Discrete action grid `{−2, 0, +2}` could not express the DA commitment of `+1 MW` when `+2 MW` violated SOC and `0 MW` was the only feasible candidate.
2. No cross-temporal coordination: cheap energy was not pre-positioned before known spike windows.
3. Deviation settlement turned a physical constraint into a **$800/MWh marginal penalty**.

**Lesson:** Myopic optimization in storage is not sub-optimal—it can be **structurally loss-making** under binding SOC constraints and DA commitment mismatch.

---

### Phase C — Scenario B: Look-Ahead Heuristic Success (Alpha Capture)

To recover from the Hour 14 shock, we implemented a **Look-Ahead Heuristic Prep-Charging Strategy**:

| Hour | Role | RT Price | Action |
|---|---|---|---|
| **7** | Prep (pre-H8) | **$10/MWh** | Aggressive charge `−P_max` |
| **8** | Spike capture | $600/MWh | Full discharge `+P_max` |
| **13** | Prep (pre-H14) | **$10/MWh** | Aggressive charge `−P_max` |
| **14** | Spike capture | **$800/MWh** | Full discharge `+P_max` |

This enables **cross-temporal energy shifting**: buy low at Hour 7/13, sell high at Hour 8/14, while maintaining deviation discipline vs. the DA schedule.

| Metric | Reference Backtest Result |
|---|---|
| Total Baseline P&L | **−$110.00** |
| Total Optimized P&L | **+$4,372.94** |
| **Net Alpha Generated** | **+$4,482.94** |
| Hour 14 Dispatch | **`+2.0 MW`** (full discharge) |
| Hour 14 Deviation | **`+1.0 MW`** (over-delivery credit) |

**Lesson:** Storage alpha is not a single-hour optimization problem—it is a **multi-period inventory problem** with a dual-settlement cash flow stack. Foresight (even heuristic) converts SOC binding from a risk into a positioning tool.

---

## ⚙️ Market Mechanics & Mathematical Formulations

---

### Industrial Sign Convention

| Operation | Power Sign | Interpretation |
|---|---|---|
| **Discharge** (sell to grid) | `P > 0` | Positive MW |
| **Charge** (buy from grid) | `P < 0` | Negative MW |
| **Idle** | `P = 0` | No dispatch |

All revenue, deviation, and P&L terms inherit this convention consistently across the codebase.

---

### Dual-Settlement P&L Decomposition

Total hourly profit and loss is decomposed into **Day-Ahead commitment value**, **real-time deviation settlement**, **RT physical revenue**, and **battery wear-and-tear**:

$$
\Pi_{\text{Total}} = \underbrace{P_{\text{RT}} \cdot \lambda_{\text{RT}}}_{\text{RT Physical Revenue}} + \underbrace{(P_{\text{RT}} - P_{\text{DA}}) \cdot \lambda_{\text{RT}}}_{\text{RT Deviation Settlement}} - \underbrace{C_{\text{wear}} \cdot |P_{\text{RT}}| \cdot \Delta t}_{\text{Degradation Cost}}
$$

Equivalently, in the workbench's implemented kernel:

$$
\Pi_{\text{Total}} = P_{\text{RT}} \cdot \lambda_{\text{RT}} + (P_{\text{RT}} - P_{\text{DA}}) \cdot \lambda_{\text{RT}} - C_{\text{wear}} |P_{\text{RT}}| \Delta t
$$

Where:

| Symbol | Definition |
|---|---|
| $P_{\text{DA}}$ | Day-ahead committed position (MW) |
| $P_{\text{RT}}$ | Realized physical dispatch (MW) |
| $\lambda_{\text{RT}}$ | Real-time LMP ($/MWh) |
| $C_{\text{wear}}$ | Battery wear-and-tear cost ($/MWh) |
| $\Delta t$ | Dispatch interval duration (hours) |

**Deviation Settlement** (PJM-style):

$$
\text{DevSettlement} = (P_{\text{RT}} - P_{\text{DA}}) \cdot \lambda_{\text{RT}}
$$

- $P_{\text{RT}} > P_{\text{DA}}$ → positive credit (over-delivery at RT)
- $P_{\text{RT}} < P_{\text{DA}}$ → negative charge (under-delivery penalty at RT)

---

### Progressive State-of-Charge (SOC) Dynamics

Let $\text{SOC}_t \in [0,1]$ denote state-of-charge as a fraction of nameplate energy capacity $E_{\max}$ (MWh). With round-trip efficiency $\eta_{\text{RT}}$ and symmetric one-way efficiencies:

$$
\eta_{\text{ch}} = \sqrt{\eta_{\text{RT}}}, \qquad \eta_{\text{dis}} = \sqrt{\eta_{\text{RT}}}
$$

The progressive SOC update over interval $\Delta t$ follows:

**Charge** ($P_t < 0$):

$$
\text{SOC}_{t+1} = \text{SOC}_t + \frac{|P_t| \cdot \eta_{\text{ch}} \cdot \Delta t}{E_{\max}}
$$

**Discharge** ($P_t > 0$):

$$
\text{SOC}_{t+1} = \text{SOC}_t - \frac{P_t \cdot \Delta t}{\eta_{\text{dis}} \cdot E_{\max}}
$$

**Idle** ($P_t = 0$): $\text{SOC}_{t+1} = \text{SOC}_t$

> **Implementation Note:** The deployed kernel parameterizes a configurable one-way efficiency $\eta$ (default `0.85`) applied directly on charge paths and inversely on discharge paths, normalized by sidebar **Total Energy Capacity** $E_{\max}$. This is operationally equivalent to symmetric loss allocation under the industrial sign convention.

Initial condition at Hour 0:

$$
\text{SOC}_0 = \text{SOC}_{\text{initial}} \quad \text{(set via sidebar slider)}
$$

---

### Operational Constraints

**Power envelope:**

$$
-P_{\max} \leq P_t \leq P_{\max}
$$

**SOC envelope:**

$$
\text{SOC}_{\min} \leq \text{SOC}_t \leq \text{SOC}_{\max}
$$

**Feasibility check** (enforced before dispatch acceptance):

$$
\text{SOC}_{\min} \leq \text{SOC}_t + \Delta\text{SOC}(P_t) \leq \text{SOC}_{\max}
$$

When Scenario A cannot satisfy the DA position within these bounds, dispatch defaults to $P_{\text{RT}} = 0$, and deviation penalties flow through automatically.

---

## 📊 Full-Stack Dashboard Architecture

Built with **Streamlit** (reactive UI layer) and **Plotly** (institutional visualization layer). Every sidebar slider change triggers a full backtest rerun via `run_backtest(config)` with fingerprinted chart keys for live UI refresh.

```
┌─────────────────────────────────────────────────────────────┐
│  Sidebar: Asset & Risk Configuration (Reactive Sliders)     │
│  P_max · E_max · η · SOC₀ · SOC_min · SOC_max · C_wear     │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  Simulation Layer: 24-Hour PJM Profile (DA/RT/DA Position)  │
└──────────────────────────┬──────────────────────────────────┘
                           │
              ┌────────────┴────────────┐
              ▼                         ▼
   Scenario A (Baseline)      Scenario B (Look-Ahead Optimizer)
              │                         │
              └────────────┬────────────┘
                           ▼
              Metrics: Baseline P&L · Optimized P&L · Net Alpha
                           │
         ┌─────────────────┼─────────────────┐
         ▼                 ▼                 ▼
      Tab 1             Tab 2             Tab 3
   Market Data      SOC & Dispatch    Trading Ledger
```

---

### Tab 1 — Market Data Analysis

- Dual-axis Plotly chart: **DA LMP** (primary axis) vs. **RT LMP** (secondary axis) over 24 hours
- Shaded anomaly zones for look-ahead prep hours (7, 13), spike hours (8, 14), and negative-price hour (20)
- **Non-overlapping vertical annotations** (`textangle = −90°`) with staggered paper-y positions (`y = 0.98` / `y = 0.80`) for adjacent-hour label pairs

---

### Tab 2 — Strategy & SOC Tracking

- **SOC line chart:** Scenario A vs. Scenario B end-of-hour SOC (%), with Hour 0 initial SOC anchor marker
- **Dispatch bar chart:** Grouped hourly MW commands for baseline vs. optimized paths
- Linked visual telemetry maps inventory state directly to dispatch decisions under the `+ / −` sign convention

---

### Tab 3 — Trading Log & Decision Attribution

Institutional-grade audited ledger with hour-by-hour fields:

| Column | Description |
|---|---|
| `Hour` | Interval index (0–23) |
| `DA_Price` / `RT_Price` | Settlement prices ($/MWh) |
| `DA_Position` | Committed schedule (MW) |
| `Optimized_Action` | Realized RT dispatch (MW) |
| `Deviation_Amount` | $P_{\text{RT}} - P_{\text{DA}}$ |
| `Realized_RT_Revenue` | Physical RT cash flow |
| `Deviation_Settlement` | Deviation cash flow |
| `Total_Realized_PnL` | Net hourly P&L after wear cost |

Styled with currency formatting and conditional P&L coloring (green / red).

---

### Reactive Sidebar — Parametric Stress Testing

| Parameter | Range | Default | Purpose |
|---|---|---|---|
| Max Power Capacity (MW) | 0.5 – 10.0 | 2.0 | Dispatch envelope |
| Total Energy Capacity (MWh) | 1.0 – 40.0 | 4.0 | SOC normalization |
| Round-Trip Efficiency | 0.50 – 1.00 | 0.85 | Charge/discharge loss |
| Initial SOC (fraction) | 0.00 – 1.00 | 0.50 | Hour 0 inventory |
| Min SOC Limit | 0.00 – 0.30 | 0.10 | Lower operating bound |
| Max SOC Limit | 0.70 – 1.00 | 0.90 | Upper operating bound |
| Wear-and-Tear Cost ($/MWh) | 0.00 – 100.00 | 20.00 | Degradation hurdle rate |

All parameters feed directly into `AssetConfig` → `bess_pjm_optimization_kernel()` on every Streamlit rerun.

---

## 🚀 Local Deployment Guide

### Prerequisites

- Python 3.9+
- `pip` or `conda`

---

### 1. Clone the Repository

```bash
git clone https://github.com/<your-org>/pjm-storage-workbench.git
cd pjm-storage-workbench
```

---

### 2. Create a Virtual Environment

```bash
python3 -m venv .venv
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate           # Windows
```

---

### 3. Install Dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

Core dependencies:

| Package | Purpose |
|---|---|
| `streamlit` | Reactive dashboard framework |
| `plotly` | Interactive institutional charts |
| `pandas` | Time-series data & ledger |
| `numpy` | Vectorized optimization kernel |

Optional (for Excel export extensions):

```bash
pip install openpyxl
```

---

### 4. Launch the Application

```bash
streamlit run app.py
```

Open the local URL printed in terminal (typically `http://localhost:8501`).

---

### 5. Verify Installation

Confirm the dashboard loads with:

- Three top-level metric cards (Baseline P&L · Optimized P&L · Net Alpha)
- Three navigation tabs
- Reactive sidebar sliders that update charts on drag

---

## 🎯 Quantitative Interview Case Study Guide

Use this project as a **STAR-method** narrative in energy trading, quant research, or infrastructure fund interviews.

---

### **S — Situation**

> "I built an interactive backtesting workbench for a battery asset operating in a PJM-style dual-settlement market. The asset commits to a day-ahead schedule but settles real-time deviations at RT LMPs. The core risk is that physical SOC constraints can prevent honoring DA commitments, converting operational limits into deviation penalties."

---

### **T — Task**

> "Design and demonstrate a dispatch framework that captures intra-day arbitrage alpha while quantifying deviation settlement risk—and prove the economic value of look-ahead coordination vs. myopic greedy dispatch."

---

### **A — Action**

> "I implemented two engines in a single Streamlit platform:
>
> 1. **Scenario A (Baseline):** Blind DA adherence with SOC clamp-to-zero logic.
> 2. **Scenario B (Optimizer):** A discrete RT kernel over `{−P_max, 0, +P_max}` augmented with a **look-ahead prep-charging heuristic** at Hours 7 and 13 ($10/MWh RT) before known spike windows at Hours 8 and 14.
>
> I enforced an industrial sign convention (+MW discharge, −MW charge), modeled deviation settlement as `(Actual − DA) × RT`, and wired all asset parameters to reactive sidebar sliders for live stress testing.
>
> When the myopic engine produced **−$1,390 Net Alpha** at Hour 14 due to SOC depletion, I diagnosed the failure mode—discrete action grid mismatch plus absent cross-temporal inventory planning—and deployed the look-ahead heuristic, recovering **+$4,482.94 Net Alpha**."

---

### **R — Result**

> "The platform demonstrates three interview-ready conclusions:
>
> 1. **Dual-settlement markets punish physical defaults**—SOC binding is a financial risk, not just an engineering constraint.
> 2. **Storage optimization is multi-period**—myopic hour-by-hour dispatch can destroy value even when RT prices appear favorable locally.
> 3. **Heuristic foresight generates measurable alpha**—cross-temporal energy shifting from $10/MWh prep hours to $800/MWh spike hours is the core value proposition of battery storage in nodal markets.
>
> The dashboard provides full P&L attribution, making the trade legible to a portfolio manager, risk officer, or hiring committee in real time."

---

### Suggested Follow-Up Questions (Be Ready)

- *Why does deviation settlement use RT price rather than DA price for the incremental MWh?*
- *What happens at Hour 20 when RT price goes negative with a committed charge position?*
- *How would you extend this from heuristic look-ahead to stochastic dynamic programming?*
- *What is the marginal value of perfect price foresight vs. one-hour look-ahead?*

---

## Project Structure

```
pjm-storage-workbench/
├── app.py              # Full-stack application (simulation, engines, UI)
├── requirements.txt    # Python dependencies
└── README.md           # This document
```

---

## Disclaimer

This workbench uses **simulated PJM-style market data** for research, education, and interview demonstration purposes. It does not constitute trading advice, market forecasting, or a production dispatch system. Actual PJM settlement rules, tariff structures, and ISO operational requirements may differ.

---

*Built for quantitative energy finance portfolios · Dual-settlement · Storage arbitrage · Deviation risk*
