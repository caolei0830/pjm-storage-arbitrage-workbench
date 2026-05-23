# PJM Spot Trading & Storage Arbitrage Quantitative Workbench  
# PJM现货交易与储能套利量化工作台

> **English:** A production-ready, interactive mathematical simulation and financial backtesting terminal for **grid-scale battery storage energy arbitrage** under PJM-style dual-settlement market rules.  
> **中文：** 面向 **电网级储能套利** 的生产级交互式数学仿真与金融回测终端，完整建模 PJM 双结算市场机制。

[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-blue.svg)](https://www.python.org/)
[![Streamlit](https://img.shields.io/badge/Streamlit-Interactive%20UI-FF4B4B.svg)](https://streamlit.io/)
[![License: Portfolio](https://img.shields.io/badge/Use-Research%20%26%20Portfolio-green.svg)](#)

---

## Table of Contents / 目录

| # | Section | 章节 |
|---|---|---|
| 1 | [Project Overview](#-1-project-title--overview--项目命名与概述) | 项目概述 |
| 2 | [Core Mathematical Framework](#-2-core-mathematical-framework--核心数学建模与状态方程) | 核心数学建模 |
| 3 | [3-Tier Production Data Pipeline](#-3-3-tier-production-data-pipeline--三轨工业级数据管线架构) | 三轨数据管线 |
| 4 | [Financial Attribution & Accounting](#-4-financial-attribution--accounting--财务归因与核算账本) | 财务归因账本 |
| — | [Quick Start](#-quick-start--快速启动) | 快速启动 |
| — | [Repository Structure](#-repository-structure--仓库结构) | 仓库结构 |
| — | [Disclaimer](#-disclaimer--免责声明) | 免责声明 |

---

## 🚀 1. Project Title & Overview / 项目命名与概述

### English

The **PJM Spot Trading & Storage Arbitrage Quantitative Workbench** is an institutional-grade analytics platform built for battery storage operators, quantitative researchers, and energy trading desks. It simulates a full **24-hour dispatch cycle** in a nodal, dual-settlement market: participants commit to a **Day-Ahead (DA)** schedule, then settle **Real-Time (RT)** deviations against Locational Marginal Prices (LMPs) as physical operations unfold.

The workbench runs two competing dispatch engines in parallel—**Scenario A (Blind DA Adherence)** vs. **Scenario B (Look-Ahead RT Optimization)**—and surfaces full profit-and-loss attribution through a reactive **bilingual Streamlit dashboard** (English primary · Chinese secondary).

| Capability | Description |
|---|---|
| **Dual-engine backtest** | Baseline compliance vs. adaptive RT optimization with heuristic foresight |
| **Industrial sign convention** | `+MW = Discharge (Sell)` · `−MW = Charge (Buy)` — enforced end-to-end |
| **SOC-aware physics** | Capacity-normalized state evolution with efficiency loss and bound constraints |
| **Multi-source data pipeline** | Simulation sandbox · live PJM bulk ingest · custom CSV stress-testing |
| **Full P&L ledger** | RT physical revenue · deviation settlement · battery wear-and-tear decomposition |

**Core research question / 核心研究命题:**

> Does intelligent real-time optimization generate alpha—or does myopic dispatch, coupled with SOC binding constraints, destroy it through deviation penalties?  
> 智能实时优化能否创造 Alpha？抑或短视调度在 SOC 约束下因偏差结算惩罚而摧毁价值？

---

### 中文

**PJM现货交易与储能套利量化工作台** 是一款面向储能运营商、量化研究员与能源交易台的机构级分析平台。系统在节点双结算市场中仿真完整 **24 小时调度周期**：市场参与者先提交 **日前 (DA)** 计划，再按 **实时 (RT)** 偏差与节点边际电价 (LMP) 进行结算。

平台并行运行两套调度引擎——**场景 A（盲目日前 adherence）** 与 **场景 B（前瞻实时优化）**——并通过响应式 **中英双语 Streamlit 仪表盘**（英文主标题 · 中文副标题）输出完整损益归因。

---

## 🔋 2. Core Mathematical Framework / 核心数学建模与状态方程

### 2.1 Industrial Sign Convention / 工业符号约定

| Operation 操作 | Power Sign 功率符号 | Interpretation 含义 |
|---|---|---|
| **Discharge** 放电 (sell 卖电) | `P > 0` | Positive MW 正兆瓦 |
| **Charge** 充电 (buy 买电) | `P < 0` | Negative MW 负兆瓦 |
| **Idle**  idle  idle | `P = 0` | No dispatch 无调度 |

All revenue, deviation, and P&L terms inherit this convention consistently across the codebase.  
所有收益、偏差与损益项在代码库中统一遵循该约定。

---

### 2.2 Dynamic Look-Ahead Rolling Optimization / 动态前瞻滚动优化

**English:** Scenario B implements a **rolling discrete optimization kernel** over the action grid `{−P_max, 0, +P_max}` at each hour `t`, augmented with a **heuristic look-ahead layer** that coordinates cross-temporal inventory positioning before known RT price spikes.

**中文：** 场景 B 在每小时 `t` 对动作空间 `{−P_max, 0, +P_max}` 执行 **滚动离散优化**，并叠加 **启发式前瞻层**，在已知 RT 尖峰窗口前协调跨时段库存布局。

#### Look-Ahead State Machine / 前瞻状态机

| Hour 小时 | Role 角色 | RT Context 实时背景 | Heuristic Action 启发式动作 |
|---|---|---|---|
| **7** | Prep (pre-H8) 预备 | Cheap RT ($10/MWh) 低价 RT | Aggressive charge `−P_max` 激进充电 |
| **8** | Spike capture 尖峰捕获 | RT spike ($600/MWh) RT 尖峰 | Full discharge `+P_max` 满功率放电 |
| **13** | Prep (pre-H14) 预备 | Cheap RT ($10/MWh) 低价 RT | Aggressive charge `−P_max` 激进充电 |
| **14** | Spike capture 尖峰捕获 | RT spike ($800/MWh) RT 尖峰 | Full discharge `+P_max` 满功率放电 |

For all other hours, the kernel evaluates feasible candidates and selects the action maximizing a **composite score** (RT revenue + deviation settlement − wear cost + shadow value bonus if the next hour is a foreseen spike).  
其余小时评估可行候选动作，最大化 **综合得分**（RT 收益 + 偏差结算 − 损耗成本 + 下一尖峰小时的影子价值奖励）。

#### Shadow Value Incentive / 影子价值激励

When hour `t+1` is a known spike with RT price `λ_{t+1}^{RT} > λ_t^{RT}`, a bonus proportional to projected SOC is added to the objective, rewarding inventory retention before the spike window:

当 `t+1` 为已知尖峰且 `λ_{t+1}^{RT} > λ_t^{RT}` 时，按投影 SOC 比例增加奖励项，激励在尖峰前保留库存：

$$
\text{Bonus}_t = 0.05 \cdot \text{SOC}_{t+1}^{\text{proj}} \cdot (\lambda_{t+1}^{\text{RT}} - \lambda_t^{\text{RT}})
$$

This converts SOC binding from a **risk constraint** into a **positioning tool** for cross-temporal arbitrage.  
这将 SOC 约束从 **风险边界** 转化为跨时段套利的 **布局工具**。

---

### 2.3 State-of-Charge (SOC) Evolution / 荷电状态演化方程

Let `SOC_t ∈ [0, 1]` denote normalized state-of-charge, `E_max` nameplate energy capacity (MWh), `η` round-trip efficiency, and `Δt` the dispatch interval (hours). Decompose charge and discharge power as non-negative components `P_t^{ch}, P_t^{dis} ≥ 0`:

令 `SOC_t ∈ [0, 1]` 为归一化荷电状态，`E_max` 为额定容量 (MWh)，`η` 为往返效率，`Δt` 为调度间隔 (小时)。将充放电功率分解为非负分量 `P_t^{ch}, P_t^{dis} ≥ 0`：

$$
\text{SOC}_{t+1} = \text{SOC}_t + \frac{\eta \cdot P_t^{\text{ch}} \cdot \Delta t - P_t^{\text{dis}} \cdot \Delta t / \eta}{E_{\max}}
$$

**Unified signed-power form (implemented in `project_soc()`):**  
**统一符号功率形式（`project_soc()` 实现）：**

With industrial convention `P_t = P_t^{\text{dis}} - P_t^{\text{ch}}` (discharge positive, charge negative):

在工业约定 `P_t = P_t^{\text{dis}} - P_t^{\text{ch}}`（放电为正、充电为负）下：

$$
\text{SOC}_{t+1} = \text{SOC}_t - \frac{P_t \cdot \Delta t \cdot \eta}{E_{\max}} \quad (P_t < 0, \text{ charge 充电})
$$

$$
\text{SOC}_{t+1} = \text{SOC}_t - \frac{P_t \cdot \Delta t}{\eta \cdot E_{\max}} \quad (P_t > 0, \text{ discharge 放电})
$$

**Operational constraints / 运行约束:**

$$
-P_{\max} \leq P_t \leq P_{\max}, \qquad \text{SOC}_{\min} \leq \text{SOC}_t \leq \text{SOC}_{\max}
$$

When Scenario A cannot honor `DA_Position` within SOC bounds, dispatch defaults to `0 MW`, triggering automatic deviation penalties.  
当场景 A 无法在 SOC 边界内履行 `DA_Position` 时，调度默认为 `0 MW`，自动触发偏差惩罚。

---

### 2.4 Linear Programming Objective / 线性规划目标函数

At each rolling step, Scenario B maximizes the hourly economic surplus over feasible actions:

场景 B 在每一步滚动优化中，对可行动作最大化小时经济 surplus：

$$
\max \sum_{t=0}^{23} \left( \lambda_t^{\text{RT}} \cdot P_t^{\text{dis}} - \lambda_t^{\text{RT}} \cdot P_t^{\text{ch}} - C_{\text{wear}} \cdot |P_t| \cdot \Delta t \right)
$$

Equivalently, under the signed-power convention with dual-settlement cash flows:

等价地，在符号功率约定与双结算现金流框架下：

$$
\max \sum_t \left( \underbrace{P_t \cdot \lambda_t^{\text{RT}}}_{\text{RT Physical Revenue 实时物理收益}} + \underbrace{(P_t - P_t^{\text{DA}}) \cdot \lambda_t^{\text{RT}}}_{\text{Deviation Settlement 偏差结算}} - \underbrace{C_{\text{wear}} |P_t| \Delta t}_{\text{Wear Cost 损耗成本}} \right)
$$

Where `λ_t^{RT}` is the Real-Time LMP ($/MWh), `P_t^{DA}` the Day-Ahead committed position (MW), and `C_wear` the battery degradation hurdle rate ($/MWh).  
其中 `λ_t^{RT}` 为实时 LMP ($/MWh)，`P_t^{DA}` 为日前承诺仓位 (MW)，`C_wear` 为电池折旧门槛费率 ($/MWh)。

The deployed kernel discretizes this objective over `{−P_max, 0, +P_max}` with vectorized NumPy scoring per hour.  
部署内核在 `{−P_max, 0, +P_max}` 上离散化该目标，并以 NumPy 向量化逐小时评分。

---

## 📡 3. 3-Tier Production Data Pipeline / 三轨工业级数据管线架构

The workbench implements a **three-mode ingestion architecture** designed for algorithmic validation, live market connectivity, and offline stress-testing—without breaking UI reactivity across Tabs 1–3.

工作台实现 **三模式接入架构**，支持算法验证、实时市场连接与离线压力测试，且不影响 Tab 1–3 的 UI 响应性。

```
┌──────────────────────────────────────────────────────────────────────┐
│  Sidebar: Market Data Mode (市场数据模式)                              │
└───────────────────────────────┬──────────────────────────────────────┘
                                │
        ┌───────────────────────┼───────────────────────┐
        ▼                       ▼                       ▼
  Mode 1: Simulation      Mode 2: Live REST       Mode 3: Custom CSV
  模拟沙盒                  线上实时流 + 灾备          本地上传压力测试
        │                       │                       │
        └───────────────────────┼───────────────────────┘
                                ▼
              Local 24-Hour Slice → run_backtest() → Tabs 1 / 2 / 3
              本地24小时切片 → 回测引擎 → 三标签页输出
```

---

### Mode 1 — Simulation Sandbox / 模拟沙盒

**English:** Injects predictable **high-volatility spike scenarios** into a controlled 24-hour profile for rigorous algorithmic tracking validation. Stress hours include look-ahead prep (H7, H13 at RT $10/MWh), RT spikes (H8 at $600, H14 at $800), and negative-price charging (H20 at −$100/MWh).

**中文：** 在可控 24 小时剖面中注入可预测的 **高波动尖峰场景**，用于严格的算法跟踪验证。压力小时包括前瞻预备充电 (H7/H13, RT $10/MWh)、RT 尖峰 (H8 $600, H14 $800) 与负电价充电 (H20 −$100/MWh)。

| Property 属性 | Value 值 |
|---|---|
| Data source 数据源 | Internal stress-test profile 内部压力测试剖面 |
| Use case 用途 | Baseline alpha capture narrative · engine regression 基准 Alpha 叙事 · 引擎回归 |
| Network I/O 网络 I/O | None 无 |

---

### Mode 2 — Live REST Feed with Automated Fallback / 线上实时流与灾备熔断

**English:** Ingests PJM **bulk historical buffers** in a single REST round-trip (`rowCount=500`, Western Hub Pnode **51217**), cached for 1 hour via `@st.cache_data`. Subsequent slider tuning and UI reruns perform **local Pandas date-chunking** with zero additional API calls—immunizing the app against PJM rate-limiting.

A **`try-except` network safeguard** wraps the entire ingest path. On TLS/handshake blockages or gateway failures, the pipeline seamlessly injects a **high-fidelity shadow snapshot** (overnight wind glut at H3 = −$12.50/MWh · evening peak at H18 = $260/MWh) and displays an amber bilingual warning—guaranteeing **100% business continuity** without white-screen crashes.

**中文：** 单次 REST 往返批量摄取 PJM **历史缓冲**（`rowCount=500`，西部枢纽节点 **51217**），经 `@st.cache_data` 缓存 1 小时。后续滑块调参与 UI 重跑仅执行 **本地 Pandas 日期切片**，零额外 API 调用——免疫 PJM 限流封禁。

全链路 **`try-except` 网络熔断** 包裹摄取路径。TLS/握手阻断或网关故障时，无缝注入 **高保真影子快照**（H3 夜间风电过剩 −$12.50/MWh · H18 晚高峰 $260/MWh），并显示琥珀色双语警告——保证 **100% 业务连续性**，无白屏崩溃。

| Stage 阶段 | Mechanism 机制 |
|---|---|
| Primary ingest 主摄取 | Data Miner 2 export URL → REST API fallback 导出 URL → REST 回退 |
| Bulk buffer 批量缓冲 | 500 hourly rows · sorted by `datetime_beginning_utc` 500 行 · 时间升序 |
| Local slice 本地切片 | Latest complete 24-hour DA+RT block 最新完整 24 小时 DA+RT 块 |
| Circuit breaker 熔断 | TLS failure → historic shadow snapshot + `st.warning()` TLS 失败 → 历史快照 + 警告 |

---

### Mode 3 — Custom Stress-Testing Upload / 本地上传压力测试

**English:** Direct drag-and-drop support for raw PJM official CSV exports (e.g. `da_hrl_lmps.csv`). The parser:

1. Normalizes column names to lowercase  
2. Filters to Pnode **51217** (or dominant node if absent)  
3. Maps `total_lmp_da` → `DA_Price`  
4. Sorts chronologically and extracts the latest 24-hour block  

When only a Day-Ahead file is uploaded (no `total_lmp_rt`), the pipeline employs a **File-Hash Based Seeding** algorithm:

```python
upload_seed = int(md5(file_bytes).hexdigest()[:8], 16) % (2**32)
np.random.seed(upload_seed)
RT_Price = DA_Price * np.random.uniform(0.8, 1.3, len(df))
RT_Price[Hour == 18] = 250.00  # injected peak spike 注入尖峰
```

This guarantees **stable, repeatable RT noise vectors** across reruns for the same file—essential for reproducible backtests and portfolio demonstrations.

**中文：** 支持拖拽原始 PJM 官方 CSV（如 `da_hrl_lmps.csv`）。解析器：列名小写标准化 → 过滤节点 51217（或众数节点）→ 映射 `total_lmp_da` → 时间排序并提取最新 24 小时块。

仅上传 DA 文件时，采用 **文件哈希确定性种子** 算法合成 RT 曲线，并对 H18 注入 **$250/MWh** 固定尖峰，确保同一文件跨 rerun 的 **稳定可复现 RT 噪声向量**——满足回测可复现性与作品集演示需求。

| Property 属性 | Value 值 |
|---|---|
| Supported formats 支持格式 | `da_hrl_lmps.csv` · `rt_hrl_lmps.csv` · combined 合并 |
| RT synthesis RT 合成 | MD5-seed `uniform(0.8, 1.3)` + H18 $250 spike MD5 种子 + H18 尖峰 |
| Reproducibility 可复现性 | Deterministic per upload hash 按上传哈希确定性 |

---

## 📊 4. Financial Attribution & Accounting / 财务归因与核算账本

### 4.1 Dual-Settlement P&L Stack / 双结算损益栈

Total hourly profit and loss is decomposed into three orthogonal cash-flow vectors:

小时总损益分解为三个正交现金流向量：

$$
\Pi_{\text{Total}} = \underbrace{P_{\text{RT}} \cdot \lambda_{\text{RT}}}_{\text{RT Physical Revenue 实时物理收益}} + \underbrace{(P_{\text{RT}} - P_{\text{DA}}) \cdot \lambda_{\text{RT}}}_{\text{Deviation Settlement 偏差结算}} - \underbrace{C_{\text{wear}} \cdot |P_{\text{RT}}| \cdot \Delta t}_{\text{Wear Cost 损耗成本}}
$$

| Symbol 符号 | Definition 定义 |
|---|---|
| `P_DA` | Day-Ahead committed position (MW) 日前承诺仓位 |
| `P_RT` | Realized physical dispatch (MW) 实际物理调度 |
| `λ_RT` | Real-Time LMP ($/MWh) 实时节点电价 |
| `C_wear` | Battery wear-and-tear cost ($/MWh) 电池折旧成本 |

---

### 4.2 Tab 3 — Deviation Settlement Mechanism / 偏差结算机制

**English:** Tab 3 (**Trading Log & Decision Attribution / 交易日志与决策归因**) provides an institutional-grade audited ledger that **separates profit vectors** between:

- **Day-Ahead committed positions** — the schedule the asset promised to the market  
- **Real-Time adaptive balancing deviations** — the incremental MWh settled at RT LMP when physical dispatch diverges from the DA schedule  

**Deviation Settlement formula / 偏差结算公式:**

$$
\text{DevSettlement}_t = (P_{\text{RT},t} - P_{\text{DA},t}) \cdot \lambda_{\text{RT},t}
$$

| Scenario 场景 | Interpretation 含义 |
|---|---|
| `P_RT > P_DA` | Over-delivery credit at RT 超交付 · RT 价格信用 |
| `P_RT < P_DA` | Under-delivery penalty at RT 欠交付 · RT 价格惩罚 |
| `P_RT = 0`, `P_DA > 0` | SOC-bound default → maximum deviation loss SOC 约束违约 → 最大偏差损失 |

**中文：** Tab 3 提供机构级审计账本，**分离** 两类利润向量：

- **日前承诺仓位** — 资产向市场承诺的调度计划  
- **实时自适应平衡偏差** — 物理调度偏离 DA 计划时，按 RT LMP 结算的增量 MWh  

This attribution makes the economic legibility of storage trading transparent to portfolio managers, risk officers, and hiring committees—in real time, hour by hour.  
该归因机制使储能交易的经济逻辑对投资组合经理、风控官与面试官 **逐小时透明可审计**。

#### Ledger Columns / 账本字段

| Column 列名 | Description 说明 |
|---|---|
| `Hour` | Interval index (0–23) 时段索引 |
| `DA_Price` / `RT_Price` | Settlement prices ($/MWh) 结算电价 |
| `DA_Position` | Committed schedule (MW) 日前承诺 |
| `Optimized_Action` | Realized RT dispatch (MW) 优化后 RT 调度 |
| `Deviation_Amount` | `P_RT − P_DA` 偏差量 |
| `Realized_RT_Revenue` | Physical RT cash flow 实时物理现金流 |
| `Deviation_Settlement` | Deviation cash flow 偏差结算现金流 |
| `Total_Realized_PnL` | Net hourly P&L after wear 扣损耗后净损益 |

---

### 4.3 Reference Backtest Narrative / 参考回测叙事

| Phase 阶段 | Strategy 策略 | Net Alpha 净 Alpha |
|---|---|---|
| **A — Baseline** 基准 | Blind DA adherence 盲目日前 adherence | Exposed to RT spikes 暴露于 RT 尖峰 |
| **B — Myopic Failure** 短视失败 | Greedy `{−P, 0, +P}` without foresight 无前瞻贪婪 | **−$1,390** (H14 SOC-bound) |
| **C — Look-Ahead Success** 前瞻成功 | Prep charge H7/H13 · spike discharge H8/H14 预备充电 · 尖峰放电 | **+$4,483** (reference config 参考配置) |

> **Lesson / 结论:** Storage alpha is a **multi-period inventory problem**, not a single-hour greedy optimization. Foresight converts SOC binding from risk into positioning.  
> 储能 Alpha 是 **多周期库存问题**，而非单小时贪婪优化。前瞻将 SOC 约束从风险转化为布局优势。

---

## 🛠 Quick Start / 快速启动

### Prerequisites / 前置条件

- Python 3.9+  
- `pip`

### Install & Launch / 安装与启动

```bash
git clone https://github.com/caolei0830/pjm-storage-arbitrage-workbench.git
cd pjm-storage-arbitrage-workbench

python3 -m venv .venv
source .venv/bin/activate          # macOS / Linux
# .venv\Scripts\activate             # Windows

pip install --upgrade pip
pip install -r requirements.txt

streamlit run app.py
```

Open the local URL printed in terminal (typically `http://localhost:8501`).  
打开终端打印的本地 URL（通常为 `http://localhost:8501`）。

### Dependencies / 依赖

| Package | Purpose 用途 |
|---|---|
| `streamlit` | Reactive bilingual dashboard 响应式双语仪表盘 |
| `plotly` | Institutional interactive charts 机构级交互图表 |
| `pandas` | Time-series ingestion & ledger 时序摄取与账本 |
| `numpy` | Vectorized optimization kernel 向量化优化内核 |
| `requests` | PJM Data Miner REST pipeline PJM 数据管线 |

---

## 📁 Repository Structure / 仓库结构

```
pjm-storage-arbitrage-workbench/
├── app.py              # Simulation · optimization · 3-tier pipeline · bilingual UI
│                       # 仿真 · 优化 · 三轨管线 · 双语界面
├── requirements.txt    # Python dependencies 依赖清单
└── README.md           # This document 本文档
```

---

## ⚠️ Disclaimer / 免责声明

**English:** This workbench uses simulated, cached, and user-uploaded PJM-style market data for **research, education, and portfolio demonstration** purposes. It does not constitute trading advice, market forecasting, or a production ISO dispatch system. Actual PJM settlement rules, tariff structures, and operational requirements may differ.

**中文：** 本工作台使用仿真、缓存及用户上传的 PJM 风格市场数据，仅供 **研究、教育与作品集展示**。不构成交易建议、市场预测或生产级 ISO 调度系统。实际 PJM 结算规则、电价结构与运营要求可能有所不同。

---

*Built for quantitative energy finance portfolios · Dual-settlement · Storage arbitrage · Deviation risk*  
*面向量化能源金融作品集 · 双结算 · 储能套利 · 偏差风险*
