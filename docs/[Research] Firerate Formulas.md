# [Research] Firerate Formulas

| **Owner** | Lev Menshchikov |
| **Doc Status** | Draft v0.3 |
| **Source of truth** | `[Specification] Firerate.md` |

***

## I. Abstract

FireRate is a panic-resistance batch-auction mechanism for Gratis-to-coen and Promis-to-coen mining. The mechanism is specified at a behavioural level in `[Specification] Firerate mechanism.md`. The updated spec (§3.5.3) now also supplies the formula for the Maximum Hourly Mining Volume per Batch Window. What the spec still leaves open is the rate semantics, the math form of pro-rata, edge-case behaviour, and a quantitative manipulation-resistance argument. This document closes that remaining gap.

**Layering relative to the spec.** Section §IV.1 of this document **mirrors** §3.5.3 of the spec: composite stress score, inverted-sigmoid stress multiplier, asymmetric per-window ramp limit. It restates the spec formula in extended form (with worked examples and an explicit derivation of properties), not as an alternative. Sections §IV.2 and §IV.3 extend the spec into territory the original BRD spec does not cover.

**In scope of this iteration:**

1. The Maximum Hourly Mining Volume `V_t` as a function of Price Oracle signals - restated from spec §3.5.3 with derivation and three numeric worked examples.
2. The mapping from `V_t` to the per-token Gratis FireRate Conversion Rate and Promis FireRate Conversion Rate (`r_G`, `r_P`). The spec defines outputs only; this section supplies the formula.
3. The pro-rata capacity rule that activates when total submitted Gratis or Promis would exceed the allocation. The spec describes the rule in prose; this section supplies the formula.
4. A quantitative cost-of-attack analysis for the canonical fake-order-book vector.

**Out of scope (deferred to iteration 2):**

- Concrete numeric values for protocol parameters (`V_base`, `m_min`, `α_G`, `α_P`, weights `w_s/w_v/w_σ/w_q`, ramp caps `r_down/r_up`, midpoint `S₀`, sensitivity `k`, hard bounds `V_min/V_max`).
- The Stress Activation function (§3.1.1) that toggles FireRate between Inactive and Active; alignment between this threshold and `S₀` is noted but not parameterised.
- Reconciliation of Hourly Emission Baseline `V_base` with the Metadosis Module emission curve.
- Fallback alternatives to the sigmoid form of Step 2 (piecewise-linear and Hill-function alternatives are listed under Open Questions).

**Output of this iteration is a parametric model.** Every formula below is written against named parameters; the values themselves remain Open Questions and require calibration by a quant analyst against historical Exchange data and stress simulations.

***

## II. Problem Statement

Under normal market conditions a holder of Gratis or Promis can mine coen at 1:1 instantly via the Mining Module. This is desirable: it gives the consumer immediate access to value. It is also dangerous: in a stress event (e.g. a price drop), a coordinated rush to mine-and-sell would compound the drop, drive coen through any structural floor, and propagate panic. The economic effect mirrors a bank run.

Firerate Mechanism engages only under stress and re-routes mining through a one-hour batch auction. Within each Batch Window `t` the protocol must answer two questions:

1. **How much coen can be minted via this path in this hour?** This is `V_t`, the Maximum Hourly Mining Volume for the current Batch Window, hard-bounded by protocol parameters `V_min` and `V_max`.
2. **At what rate does each individual ticket settle?** This is `r_G` for Gratis-sourced tickets and `r_P` for Promis-sourced tickets, both expressed as `coen / source-token` and bounded in `[m_min, 1]`.

`V_t` reflects the *market's tolerance for new sell-side pressure*: tighter spread, calmer velocity, healthier volume → higher `V_t`. `r_G` and `r_P` reflect *participant-level fairness*: every ticket in the same Batch Window of the same token type settles at the same rate, scaled down only if total demand exceeds capacity.

The spec mandates three non-negotiable properties for the `V_t` formula:

- **Monotonicity** (§3.5.3): `V_t` is non-increasing in bid-ask spread, velocity, and volatility, and non-decreasing in trading volume.
- **Boundedness** (§3.5.3): `V_t ∈ [V_min, V_max]` by hard clamp.
- **Anti-step-function** (§3.5.3 Step 3): `V_t ∈ [V_{t−1} · (1 − r_down), V_{t−1} · (1 + r_up)]`.

They must also be implementable on a public, decentralised L1 with no privileged off-chain backend: deterministic, gas-bounded, fixed-point integer arithmetic.

***

## III. Inputs and Assumptions

### 1. Onchain Inputs (Price Oracle)

The mechanism consumes four market signals named in `[Specification] Firerate mechanism.md` §3.5.1. The spec explicitly marks them as *(normalized)* — meaning the Price Oracle is responsible for delivering each signal in normalized form. This document follows that convention; signals are denoted `s`, `q`, `v`, `σ` and treated as dimensionless quantities in a comparable scale.

Current implementation status, derived from `/research/outbe-feeder/` and `/research/outbe-chain/crates/system/oracle/`:

| Signal | Symbol | Spec role | Status in current Oracle | Derivation path |
| --- | --- | --- | --- | --- |
| Mid-market coen price | `p` | not consumed directly by FireRate — feeds derived signals | Direct (TVWAP/VWAP across whitelisted providers) | `IOracle.calculate_vwap(pair_id, t-Δ, t)` |
| Trading volume (normalized) | `q` | liquidity-quality signal; reduces stress when high | Raw volume direct (sum of `snapshot_volume`); normalization not implemented | Needs Oracle extension *or* derivation in FireRate from snapshot history |
| Velocity of price changes (normalized) | `v` | stress signal; monotonicity guaranteed | Not implemented | Derivation in FireRate from snapshot history: `mean(|Δp / p_{t−1}|)` over lookback window |
| Realized volatility (normalized) | `σ` | stress signal | Not implemented | Derivation in FireRate from snapshot history: `std(log(p_t / p_{t−1}))` over lookback window |
| Bid-ask spread (normalized) | `s` | primary stress signal; monotonicity guaranteed | **Not available** in current feeder/precompile | See note below |

**Bid-ask spread gap.** The current `outbe-feeder` (`src/aggregator.rs`) consumes only `(price, volume)` from each provider — bid and ask are not retained through the aggregation pipeline. The downstream `Oracle` precompile (`crates/system/oracle/src/logic.rs`) likewise has no bid/ask slot. This is a real gap relative to the spec, which names `s` as the primary signal and assumes its availability in §3.5.3 Step 1.

Two paths to close the gap:

- **(Path A — preferred long-term)** Extend the Price Oracle Integration Specification so that each provider returns and the feeder aggregates `(bid, ask)` per pair, with normalization performed by the Oracle. Spread becomes a first-class Oracle output.
- **(Path B — interim proxy)** Compute `s_proxy` onchain inside FireRate as the normalized intra-window price range: `s_proxy = (p_max(window) − p_min(window)) / p_mid(window)`. This is *not* a bid-ask spread but correlates with market stress and is monotone in the same direction. Suitable as a stop-gap while Path A is implemented.

Both paths are tracked in §VIII Open Questions.

**Where normalization happens.** The new spec's `(normalized)` annotation implies normalization is the Oracle's responsibility (henceforth referred to as **N1**). This document assumes N1. If the Oracle Integration Specification does not implement normalization in iteration 1, FireRate would need to do it itself from snapshot history (**N2**, fallback). The choice is listed under Open Questions.

**Normalized signal semantics.** A normalized signal in this document is `x̂ ∈ [0, 1]` with a uniform stress interpretation: `0` = "at long-run calm baseline", `1` = "at stress saturation (the level at which further increase should no longer affect the rate)". For volume `q` symmetrically: `0` = illiquidity, `1` = baseline liquidity or above.

The `[0, 1]` scale is required for consistency with the §3.5.3 spec formula `S_raw = w_s s + w_v v + w_σ σ − w_q q`: at calm-baseline (`s = v = σ = 0`, `q = 1`) the composition must yield non-positive `S_raw` (so `S = 0` after clamp); at full stress (`s = v = σ = 1`, `q = 0`) it approaches `Σ w_i`.

The concrete mapping from raw market data to `[0, 1]` is the Oracle's responsibility and an Open Question for calibration. One candidate: `x̂ = clamp(max(0, x_raw − x_baseline) / x_scale, 0, 1)` for stress signals, where `x_baseline` is a multi-day rolling reference and `x_scale` is the calibrated distance from baseline to saturation. Alternatives: z-score with saturation, piecewise-linear over historical percentiles.

**Acceptance criterion for calibration.** At calm-baseline (`s = v = σ = 0`, `q = 1`) the composite score must satisfy `S ≤ S₀`. Otherwise FireRate would be permanently Active on a normal market.

### 2. Protocol Parameters

Names and symbols below mirror `[Specification] Firerate mechanism.md` §3.5.1. All parameter *values* remain Open Questions for iteration 2 calibration.

| Parameter | Symbol | Role | Default placeholder |
| --- | --- | --- | --- |
| Hourly Emission Baseline | `V_base` | Reference hourly mining volume under normal conditions, in coens (1e18). The anchor for the stress multiplier. | TBD — must reconcile with Metadosis Module emission curve |
| Lower bound | `V_min` | Hard floor on `V_t`, applied after `m(S)` and before ramp limit | TBD |
| Upper bound | `V_max` | Hard ceiling on `V_t`, applied after `m(S)` and before ramp limit. Note: in this document we use `V_t` for the per-window output and reserve `V_max` exclusively for the hard ceiling, matching spec notation. | TBD |
| Stress-signal weights | `w_s, w_v, w_σ, w_q` | Weights inside the composite stress score (Step 1) | TBD; calibrated jointly |
| Stress midpoint | `S₀` | Centre of the inverted sigmoid (Step 2); aligned with the FireRate activation threshold from §3.1.1 | TBD; suggested in `[0.3, 0.6]` so the curve transitions inside the activation regime |
| Sensitivity coefficient | `k` | Sigmoid steepness around `S₀` | TBD; suggested in `[5, 20]` (higher = sharper transition) |
| Minimum multiplier | `m_min` | Lowest permitted fraction of `V_base` under severe stress; `V_t ≥ V_base · m_min` before hard bounds | TBD; suggested range 0.05–0.20 |
| Downward ramp cap | `r_down` | Maximum permitted *relative decrease* in `V_t` vs. `V_{t−1}` | TBD; typically larger than `r_up` so the protocol can tighten faster than it relaxes |
| Upward ramp cap | `r_up` | Maximum permitted *relative increase* in `V_t` vs. `V_{t−1}` | TBD; smaller than `r_down` for the reason above |
| Gratis Allocation Share | `α_G` | Fraction of `V_t` allocated to Gratis-sourced batch | TBD; `α_G + α_P = 1` |
| Promis Allocation Share | `α_P` | Fraction of `V_t` allocated to Promis-sourced batch | TBD; `α_G + α_P = 1` |
| Lookback window (only relevant under N2) | `T_lookback` | Time horizon for FireRate-side derivation of velocity, volatility, or `s_proxy` if Oracle does not deliver them normalized | TBD; suggested 1–4 hours |

### 3. Notation Conventions

- All quantities are non-negative integers in 1e18 fixed-point unless explicitly marked otherwise. Multiplications stage to 1e36 and divide back to 1e18, matching Oracle semantics (`src/contract.rs:SCALE_1E18`).
- `clamp(lo, hi, x) = min(hi, max(lo, x))`. When written `clamp(x, lo, hi)` (spec ordering), the meaning is identical.
- `sigmoid(z) = 1 / (1 + exp(−z))` is the logistic function. `sigmoid(0) = 0.5`; `sigmoid(z) → 1` as `z → +∞`; `sigmoid(z) → 0` as `z → −∞`.
- `s`, `v`, `σ`, `q` are normalized signals (see §III.1). A Batch Window is indexed by `t`. Signals are aggregated over the full window `t` (minutes 0–60) and settlement happens at the close of `t`. Under this convention the cool-down (minutes 30–60) plays a dual role: it locks ticket composition (no add/cancel after minute 30) and is also part of the signal-sampling window (signals keep accumulating), which is what makes the §3.6 manipulation-resistance argument bite. Alternative readings — instantaneous snapshot at close, sampling restricted to cancellation window only, or `t−1` data informing `t` — were considered; (b) was chosen because only it gives cool-down a non-trivial signal-side role.
- `V_t` is the Maximum Hourly Mining Volume actually applied during Batch Window `t`. `V_{t−1}` is the corresponding value from the previous Batch Window, used by the ramp limit.

***

## IV. Core Formulas

The core consists of four building blocks executed in sequence at the close of every Batch Window `t`:

1. Combine the four normalized signals into a single composite stress score `S ∈ [0, 1]`.
2. Map `S` through an inverted sigmoid to a stress multiplier `m(S) ∈ [m_min, 1]`, anchor it to `V_base`, and clamp to hard bounds `[V_min, V_max]`. Call the result `V_target`.
3. Apply an asymmetric per-window ramp limit against `V_{t−1}` to produce the final `V_t`.
4. Derive the per-token *FireRate Conversion Rate* by composing `m(S)` with a pro-rata capacity rule against `V_t · α_G` and `V_t · α_P`.

Steps 1–3 produce `V_t` and are restated from spec §3.5.3. Step 4 produces `r_G` and `r_P` and is the layer this document adds.

### 1. Maximum Hourly Mining Volume

This section mirrors spec §3.5.3 with extended derivation, monotonicity argument, and three numeric worked examples. Alternative functional forms for Step 2 (piecewise-linear, Hill-function product) are surveyed in §VIII Open Questions as fallbacks should the sigmoid form prove unsuitable in iteration 2.

#### 1.1 Step 1 — Composite stress score

```
S_raw = w_s · s + w_v · v + w_σ · σ − w_q · q
S     = clamp(S_raw, 0, 1)
```

`S = 0` corresponds to normal market conditions; `S = 1` corresponds to maximal stress. Volume `q` enters with a negative sign so that higher liquidity reduces stress. The `clamp` guards against negative `S_raw` when volume is high.

The composition is *linear in the signals*. This is a deliberate design choice: it pushes all the non-linear behaviour into Step 2 (the sigmoid), so that calibration can think of the four weights `w_i` as direct contributions to a single underlying "stress" quantity rather than as parameters of a complicated multivariate response.

Volume's negative sign carries one consequence worth flagging: a very liquid market (`q` significantly above its normalized baseline) can cancel out moderate spread/velocity/volatility stress, driving `S_raw` below zero. The `clamp` floor at 0 means `V_t` will then sit at `V_base` (the normal-conditions anchor), not above it. This matches the spec's intent — `V_base`, not `V_max`, is the normal-conditions target.

#### 1.2 Step 2 — Inverted-sigmoid stress multiplier

```
m(S)     = m_min + (1 − m_min) · (1 − sigmoid(k · (S − S₀)))
V_target = clamp(V_base · m(S), V_min, V_max)
```

This is the heart of the formula. Reading it from the inside out:

- `sigmoid(k · (S − S₀))` is a monotonically increasing S-curve in `S`, centred at `S₀`, with slope controlled by `k`. It takes values near 0 below `S₀` and near 1 above `S₀`.
- `1 − sigmoid(...)` inverts it: monotonically *decreasing* in `S`, near 1 below `S₀`, near 0 above `S₀`.
- The outer affine wrap `m_min + (1 − m_min) · (...)` rescales the range from `[0, 1]` to `[m_min, 1]`.

So `m(S) ∈ [m_min, 1]`, monotonically decreasing in `S`, with `m(0) ≈ 1`, `m(S₀) = (1 + m_min) / 2` (midpoint), and `m(1) ≈ m_min`.

Three notes on the parameter choices baked into this form:

- `V_base`, not `V_max`, is the multiplicand. Under normal conditions (`S → 0`, `m → 1`), `V_target → V_base`. `V_max` enters only as a hard ceiling via the clamp, in case `V_base · m(S)` ever exceeds it (this can happen if `V_base > V_max`, which would itself be a configuration error).
- `S₀` is aligned with the FireRate activation threshold from §3.1.1. This means the sigmoid transition happens *inside* the activation regime: the curve has the largest slope around the level of stress that just barely triggers FireRate. Below `S₀` the response is gentle (FireRate barely engaged); above `S₀` the response saturates (extreme stress quickly approaches `m_min`).
- `m_min` is a *protocol-level safety floor*, not a calibration knob. It guarantees that even under sustained extreme stress, mining is not throttled to zero — there is always at least `V_base · m_min` of mining capacity available.

**`V_min` and `m_min` are a paired calibration.** Both define a lower bound on `V_target`, but only one binds at a time: if `V_min ≤ V_base · m_min`, the sigmoid-floor `m_min` binds and `V_min` is inactive; if `V_min > V_base · m_min`, the hard floor `V_min` binds first and the effective lower bound on `m(S)` becomes `V_min / V_base` rather than `m_min`. They cannot be calibrated independently. Recommended semantics: `m_min` is the primary knob (the floor on the stress-imposed capacity reduction); `V_min` is a guard against configuration errors in `V_base` or `m_min`, calibrated as `V_min < V_base · m_min` with some margin so it only fires if governance misconfigures one of the other two.

#### 1.3 Step 3 — Asymmetric per-window ramp limit

```
V_t = clamp(V_target, V_{t−1} · (1 − r_down), V_{t−1} · (1 + r_up))
```

`r_down` and `r_up` are independent parameters. Typical calibration: `r_down > r_up`, so the protocol can tighten faster than it relaxes. This protects against the canonical attack pattern (a manipulator briefly inflates the spread to depress `V_target`, then withdraws): the asymmetry means that even if `V_target` immediately bounces back after the manipulation ends, `V_t` only climbs back up at the slower `r_up` pace.

For the first window of any Active period (first activation or re-activation after an Inactive cycle), `V_{t−1}` is undefined. The choice of this anchor is an Open Question: it determines the protocol's behaviour in the first window and shapes the "trigger activation once" attack vector. Example: FireRate active in 10:00–11:00 (final `V_10`), inactive 11:00–14:00, then active again 14:00–15:00 — what `V_{t−1}` to use for window `t = 14`?

Candidate variants:

- **(a) Stress-adjusted anchor.** `V_{t−1} := V_base · m(S(t_activate))`. Anchors the entry to current stress at activation. But at `m(S₀) = (1 + m_min) / 2 ≈ 0.55` this traps the protocol near `0.55 · V_base` for ~7 windows after even a brief spike (asymmetric ramp climbs only at `r_up` per window). Creates an attack: trigger activation once → lock capacity at half baseline for hours.
- **(b) Last active.** Re-activation only: `V_{t−1}` = last `V_t` from the previous Active period (`V_10` above). Preserves continuity but ignores structural changes during the Inactive cycle.
- **(c) Adjusted baseline.** `V_base` adjusted for stress accumulated during Inactive. Intermediate, but requires tracking signals while Inactive — contradicts the "mechanism is off" semantics.
- **(d) Baseline anchor.** `V_{t−1} := V_base` directly. If stress is real, the first window tightens via the downward ramp (`~30%`) and continues from there; if stress is false (manipulator-triggered), capacity stays at baseline. Closes the "trigger-once" vector but requires `r_down` over a single window to be a sufficient defensive response.

The choice ties into the cost-of-attack analysis in §V: how expensive it is to provoke activation, and what the protocol pays as a result. The "asymmetric ramp protects against brief manipulation" argument in §IV.1.3 only holds when `V_{t−1}` was already near `V_base` *before* the attack — on cold-start under (a), the argument does not apply. Decision deferred to §VIII Open Questions.

Note that Steps 2 and 3 each constrain `V_t` through a different mechanism — Step 2 enforces an *absolute* range `[V_min, V_max]` based on protocol-level safety bounds, Step 3 enforces a *relative* range based on the previous window's value. Both are needed: Step 2 alone permits arbitrarily fast movement inside `[V_min, V_max]`; Step 3 alone permits slow drift through configuration errors that violate `V_min` or `V_max`.

#### 1.4 Monotonicity

The spec mandates monotonicity in spread and velocity; the linear composition with positive weights gives strict monotonicity in `s`, `v`, `σ` (each increases `S_raw`, which after the `clamp` either leaves `S` unchanged at 0 or increases it strictly inside `[0, 1]`). The inverted sigmoid is strictly decreasing in `S`, so `m(S)` is strictly decreasing in any of `s`, `v`, `σ` whenever the clamp does not bind. After the `V_target` clamp and the ramp limit, `V_t` remains non-increasing in `s`, `v`, `σ` — bounds and ramps can only flatten, never reverse, the dependence.

Volume `q` enters with a negative weight, so the symmetric argument shows `V_t` is non-decreasing in `q`.

A formal proof against a fully calibrated parameter set is deferred until §VII (Acceptance criteria & invariants), to be written in a later iteration.

#### 1.5 Worked example

Placeholder parameters for arithmetic only; values are Open Questions.

```
V_base    = 1,000,000 coens / hour
V_min     = 100,000
V_max     = 1,100,000
m_min     = 0.10
S₀        = 0.40
k         = 10
r_down    = 0.30
r_up      = 0.10
weights   = (w_s, w_v, w_σ, w_q) = (0.40, 0.25, 0.20, 0.15)
V_{t−1}   = 800,000   // assumption for the ramp limit
```

For the sigmoid calculations below: `sigmoid(z) = 1 / (1 + exp(−z))`. Numeric values are rounded to 3 significant figures.

**Case 1 — Calm market** (FireRate just engaged on a faint signal).

| Signal | Normalized | Contribution to `S_raw` |
| --- | --- | --- |
| `s` | 0.20 | 0.40 · 0.20 = 0.080 |
| `v` | 0.10 | 0.25 · 0.10 = 0.025 |
| `σ` | 0.20 | 0.20 · 0.20 = 0.040 |
| `q` | 1.00 | −0.15 · 1.00 = −0.150 |

```
S_raw = 0.080 + 0.025 + 0.040 − 0.150 = −0.005
S     = clamp(−0.005, 0, 1) = 0
z     = k · (S − S₀) = 10 · (0 − 0.40) = −4
sigmoid(z) = 1 / (1 + exp(4)) ≈ 0.018
m(S)  = 0.10 + 0.90 · (1 − 0.018) = 0.10 + 0.884 = 0.984
V_target = clamp(1,000,000 · 0.984, 100k, 1.1M) = 984,000
V_t   = clamp(984,000, 800k · 0.70, 800k · 1.10) = clamp(984,000, 560k, 880k) = 880,000
```

The ramp cap binds *on the upside* — `V_target` (984k) exceeds the maximum allowed jump from `V_{t−1}` (880k upper). The protocol relaxes at the configured `r_up = 10%` and emits 880,000 this hour, with further relaxation in subsequent hours.

**Case 2 — Mild stress.**

| Signal | Normalized | Contribution to `S_raw` |
| --- | --- | --- |
| `s` | 0.80 | 0.40 · 0.80 = 0.320 |
| `v` | 0.60 | 0.25 · 0.60 = 0.150 |
| `σ` | 0.50 | 0.20 · 0.50 = 0.100 |
| `q` | 0.50 | −0.15 · 0.50 = −0.075 |

```
S_raw = 0.320 + 0.150 + 0.100 − 0.075 = 0.495
S     = clamp(0.495, 0, 1) = 0.495
z     = 10 · (0.495 − 0.40) = 0.95
sigmoid(z) ≈ 0.721
m(S)  = 0.10 + 0.90 · (1 − 0.721) = 0.10 + 0.251 = 0.351
V_target = clamp(1,000,000 · 0.351, 100k, 1.1M) = 351,000
V_t   = clamp(351,000, 560k, 880k) = 560,000
```

The ramp cap binds *on the downside* — `V_target` (351k) is below the maximum allowed drop from `V_{t−1}` (560k lower). The protocol tightens at the configured `r_down = 30%` and emits 560,000 this hour, with further tightening permitted next hour if the stress persists. After two windows, `V_t` could reach `0.70 · 560 = 392k`; after three, `0.70 · 392 = 274k`, and so on.

**Case 3 — Severe stress** (rush).

| Signal | Normalized | Contribution to `S_raw` |
| --- | --- | --- |
| `s` | 1.00 | 0.40 · 1.00 = 0.400 |
| `v` | 1.00 | 0.25 · 1.00 = 0.250 |
| `σ` | 0.80 | 0.20 · 0.80 = 0.160 |
| `q` | 0.10 | −0.15 · 0.10 = −0.015 |

```
S_raw = 0.400 + 0.250 + 0.160 − 0.015 = 0.795
S     = clamp(0.795, 0, 1) = 0.795
z     = 10 · (0.795 − 0.40) = 3.95
sigmoid(z) ≈ 0.981
m(S)  = 0.10 + 0.90 · (1 − 0.981) = 0.10 + 0.017 = 0.117
V_target = clamp(1,000,000 · 0.117, 100k, 1.1M) = 117,000
V_t   = clamp(117,000, 560k, 880k) = 560,000
```

The sigmoid sits near `m_min` saturation. The ramp cap binds on the downside again — `V_target` (117k, just above the protocol floor) is far below the maximum allowed drop from `V_{t−1}` (560k). Emission this hour is 560,000; subsequent hours can drop by up to 30% each, eventually approaching `V_base · m_min = 100,000` over several hours.

**Why the ramp limit matters in practice.** Cases 2 and 3 both produced the same `V_t = 560,000` — distinguishing only at later windows as the ramp continues to bite. This is the intended behaviour: a sudden stress spike cannot collapse capacity in a single window. The attacker cannot drive `V_t` from 800k to 100k in one hour just by briefly inflating the spread; the ramp limit forces the move to take several windows, during which the manipulation must be sustained (which is exactly what cool-down + signal-sampling are designed to make expensive — see §V).

### 2. FireRate Conversion Rate per Token

#### 2.1 Base rate semantics — two candidate models

`[Specification] Firerate mechanism.md` §3.5.2 names the per-token Conversion Rate as an output bounded by `≤ 1` but does not specify how to compute it. Two qualitatively different models are consistent with the spec; the choice between them is a product decision with direct user-facing consequences. Both are documented here; selection is listed in §VIII Open Questions.

**Model A — Universal stress penalty (`base_rate = m(S)`).** The same stress multiplier `m(S)` from §IV.1.2 is applied both to the hourly capacity and to every individual ticket:

```
base_rate(t) = m(S(t))
```

Under this model, *any* ticket settled during a stressed Batch Window pays the stress penalty in full, regardless of how many other tickets are in the same batch. If `m(S) = 0.5`, a holder who locked `1000 Gratis` burns 1000 Gratis and mines 500 coen. The 500 coen "lost" is a structural disincentive to mine during panic — it tells the holder: *wait until the market normalizes*.

Properties:

- Single parameter surface; both `V_t` and `base_rate` track the same scalar `m(S)`.
- Strongest possible deterrent against panic-mining: even a lone holder pays the full penalty.
- Cleanest economic narrative: "FireRate is a stress tax on early movers".
- Cheapest onchain: one `m(S)` value drives everything.
- **Double leverage for an attacker.** Under Model A, an attacker who suppresses `m(S)` (by inflating spread, velocity, volatility, or suppressing volume) gains leverage simultaneously on `V_t` (hourly cap) and on `base_rate` (per-token rate). Under Model B this coupling is broken: `V_t` and `base_rate` are decoupled, and the only attack vector against emission is supply-flooding (which requires committed source-token capital). This is an attack-surface asymmetry that will be quantified in §V.

**Model B — Cap-only penalty (`base_rate = 1`).** No per-ticket penalty unless the capacity cap binds. Under this model:

```
base_rate(t) = 1
```

The pro-rata cap is the only mechanism that pushes `effective_rate` below 1. A lone holder mining during stress gets 1:1; rate degrades only when the aggregate batch exceeds `V_t · α_G` (or `· α_P`).

Properties:

- More forgiving for legitimate users: a holder who needs to mine during a moderate stress event and is not part of a rush gets full value.
- The penalty is *collective*: harm scales with how many others are doing the same thing.
- Independent calibration of `V_t` (capacity) vs. effective rate (fairness). Two surfaces, but each is simpler.
- Risk: weaker disincentive against speculative early mining. A sophisticated holder who knows `V_t` and current `Q_G` can time entry to stay below the cap and avoid any penalty.

**Hybrid considered but not split out (`base_rate = f(m(S))` with `f(1) = 1`, `f(m_min) > m_min`).** A monotone function softer than identity. This is mathematically a continuum between Model A and Model B; if the project wants a middle position it adopts a specific `f`, which is in essence a re-parameterization of Model A with a flatter response. Captured as a sub-option in §VIII Open Questions.

**Working assumption for the rest of the document.** Sections §IV.2.2 onward and §V (game-theory) are written against Model A. If Model B is selected, the formulas in §2.2–2.4 still hold with `base_rate ≡ 1`; only the worked-example arithmetic needs re-running, and the cost-of-attack analysis in §V tightens (under Model B, the attacker's leverage shifts entirely to capacity inflation rather than rate suppression).

`base_rate ≤ 1` is guaranteed in both models.

#### 2.2 Pro-rata capacity cap

The Gratis and Promis allocations are partitions of `V_t`:

```
allocation_G(t) = V_t · α_G
allocation_P(t) = V_t · α_P
```

with `α_G + α_P = 1` (`[Specification] Firerate mechanism.md` §3.4). Within a Batch Window, let `Q_G` and `Q_P` denote the total Gratis and total Promis locked across all unsettled tickets at the moment of settlement.

If everyone mining at `base_rate` would already fit inside their allocation, no further adjustment is needed. If they would exceed it, the per-token rate is scaled down so that *total coen minted exactly equals allocation*. This is the proportional scaling mandated in §3.4. Combining both bounds gives the effective rate:

```
effective_rate_G(t) =
    if Q_G > 0:   min(base_rate(t), allocation_G(t) / Q_G)
    else:         base_rate(t)        // no Gratis tickets — value is irrelevant

effective_rate_P(t) =
    if Q_P > 0:   min(base_rate(t), allocation_P(t) / Q_P)
    else:         base_rate(t)
```

The two minimums are independent — Gratis oversubscription does not affect Promis settlement and vice versa.

#### 2.3 Settlement totals

Each ticket of type `GRATIS` with `amount_locked = a` burns `a` Gratis and mints `a × effective_rate_G` coen. Total coens minted in the window are therefore:

```
coens_minted_G(t) = Q_G × effective_rate_G(t) ≤ allocation_G(t)
coens_minted_P(t) = Q_P × effective_rate_P(t) ≤ allocation_P(t)
```

The inequality is tight when `Q · base_rate ≥ allocation` (cap binds), loose when `Q · base_rate < allocation` (base rate binds).

#### 2.4 Worked example

Same parameters as §IV.1.5 Case 2 (mild stress). After the ramp limit, `V_t = 560,000` and `m(S) = 0.351`. Note: `base_rate` (under Model A) tracks `m(S)`, not `V_t / V_base` — the two diverge whenever the ramp limit binds, as it does here. Allocation split `α_G = 0.7`, `α_P = 0.3`.

```
V_t          = 560,000 coen
m(S)         = 0.351   →   base_rate = 0.351 under Model A
allocation_G = 0.7 · 560,000 = 392,000 coen
allocation_P = 0.3 · 560,000 = 168,000 coen
```

**Scenario A — Light demand.** `Q_G = 200,000 Gratis`, `Q_P = 100,000 Promis`.

```
allocation_G / Q_G = 392,000 / 200,000 = 1.960
effective_rate_G   = min(0.351, 1.960) = 0.351
coens_minted_G     = 200,000 · 0.351 = 70,200

allocation_P / Q_P = 168,000 / 100,000 = 1.680
effective_rate_P   = min(0.351, 1.680) = 0.351
coens_minted_P     = 100,000 · 0.351 = 35,100
```

Both batches settle at `base_rate`. Cap is loose for both. Total coen minted: 105,300 (well below `V_t = 560,000`). The stress penalty is doing all the work.

**Scenario B — Gratis batch oversubscribed, Promis batch light.** `Q_G = 2,000,000 Gratis`, `Q_P = 100,000 Promis`.

```
allocation_G / Q_G = 392,000 / 2,000,000 = 0.196
effective_rate_G   = min(0.351, 0.196) = 0.196
coens_minted_G     = 2,000,000 · 0.196 = 392,000   ← exactly hits allocation

effective_rate_P   = min(0.351, 1.680) = 0.351
coens_minted_P     = 35,100
```

Gratis holders take a sharper cut (0.196 < 0.351) because demand exceeded allocation. Promis holders are unaffected: their cap is independent.

**Scenario C — Both batches oversubscribed.** `Q_G = 2,000,000`, `Q_P = 1,000,000`.

```
effective_rate_G = min(0.351, 392,000 / 2,000,000) = 0.196
effective_rate_P = min(0.351, 168,000 / 1,000,000) = 0.168
coens_minted_G   = 392,000
coens_minted_P   = 168,000
```

Both caps bind. Both groups absorb the full pro-rata reduction. Total coen minted = 560,000, hitting `V_t` exactly.

**Scenario D — Promis-only batch (no Gratis tickets).** `Q_G = 0`, `Q_P = 100,000`.

```
effective_rate_G = 0.351   (no tickets, value irrelevant)
effective_rate_P = min(0.351, 168,000 / 100,000) = 0.351
coens_minted_G   = 0
coens_minted_P   = 35,100
```

Gratis allocation is unused. The protocol does not redistribute the unused allocation to Promis: separation is preserved (§3.4 explicitly says "separate batch allocations").

#### 2.5 Edge cases and invariants

- `effective_rate ≥ 0` always (both arguments of `min` are non-negative; `m(S) ≥ m_min > 0`).
- `effective_rate ≤ 1` always (`base_rate ≤ 1`; capacity cap is independent).
- `coens_minted_G(t) ≤ allocation_G(t)`, `coens_minted_P(t) ≤ allocation_P(t)` — the inequalities the spec demands in §3.4.
- `coens_minted_G(t) + coens_minted_P(t) ≤ V_t` follows from the above plus `α_G + α_P = 1`.
- The locked amount is *fully burned* on settlement regardless of rate (§3.3.3). A ticket of `1000 Gratis` settled at `effective_rate = 0.5` burns 1000 Gratis and mints 500 coen — there is no Gratis refund. This is a deliberate design property: it is the friction that disincentivizes panic-mining.

***

## V. Quantitative Game Theory

*(To be filled in.)*

***

## VI. Computation Flow

*(To be filled in.)*

***

## VII. Acceptance Criteria and Invariants

*(To be filled in.)*

***

## VIII. Open Questions

### 1. Parameter calibration (deferred to quant analyst)

All numeric values for the parameters listed in §III.2 remain open and must be calibrated against historical Exchange data, oracle backtesting, and stress simulations. In rough order of leverage on protocol behaviour:

- `V_base` — and its alignment with the Metadosis Module emission curve.
- `m_min` — the protocol-level floor on the stress multiplier.
- `S₀` — the stress midpoint of the sigmoid. Spec mandates alignment with the FireRate activation threshold from §3.1.1; both values must be calibrated jointly.
- `k` — the sigmoid steepness; controls how sharply the protocol transitions from "normal" to "stressed" behaviour.
- `w_s, w_v, w_σ, w_q` — signal weights in the composite stress score.
- `r_down, r_up` — asymmetric ramp limits between consecutive Batch Windows. Recommended `r_down > r_up` but values open.
- `α_G, α_P` — Gratis vs. Promis allocation shares, with `α_G + α_P = 1`.
- `V_min, V_max` — hard absolute bounds. **`V_min` is paired with `m_min`**: only one binds at a time (see §IV.1.2).

### 2. Rate model — Model A vs. Model B vs. hybrid

Section §IV.2.1 documents both candidate models for `base_rate` (Universal Stress Penalty `= m(S)` vs. Cap-only `= 1`) plus a hybrid family `f(m(S))`. The spec does not constrain this choice. It must be made by Product before the formula goes live — the user-facing implication is materially different.

### 3. Stress Activation function (§3.1.1)

The Active/Inactive toggle is still TBD in the spec. Until that function is defined, FireRate cannot deterministically engage. Two design dimensions are open: (a) the form of the threshold function (single-signal vs. composite, hysteretic vs. instantaneous), and (b) the alignment between the activation threshold and the `S₀` parameter of the rate formula (the spec already mandates alignment but does not parameterise it).

### 3a. Cold-start and re-activation: `V_{t−1}` on the first window of an Active period

Combined question for first activation and re-activation after an Inactive cycle. Variants: (a) stress-adjusted (`V_base · m(S(t_activate))` — current placeholder, but opens a "trigger-once" attack vector), (b) last active `V_t` from the previous Active period (re-activation only), (c) `V_base` adjusted for stress accumulated during Inactive, (d) `V_base` directly (closes "trigger-once", relies on `r_down` for real stress response). Tied to §V cost-of-attack. See §IV.1.3 for the full discussion.

### 4. Bid-ask spread availability in Price Oracle

Spec §3.5.1 names `s` as the primary stress signal. The current `outbe-feeder` does not aggregate bid/ask. Two paths to close the gap, documented in §III.1:

- **Path A** — extend Price Oracle Integration Specification so that bid/ask is delivered as a first-class Oracle output.
- **Path B** — interim proxy: `s_proxy = (p_max(window) − p_min(window)) / p_mid(window)`, computed by FireRate from snapshot history.

Both paths satisfy the formula; the question is engineering cost and time-to-deploy. Decision required from the Price Oracle owner.

### 5. Normalization scheme (calibration-critical)

Spec §3.5.1 annotates signals as `(normalized)` but does not specify the normalization. The chosen scheme governs the interpretation of weights `w_i` and `S₀`, so it belongs in the calibration group, not the architectural one. Open items:

- **Mapping formula.** Candidates: excess-over-baseline scaled (`x̂ = clamp(max(0, x_raw − x_baseline) / x_scale, 0, 1)`), z-score with saturation, piecewise-linear over historical percentiles.
- **Baseline window length** for the rolling reference.
- **`x_scale`** — the calibrated distance from baseline to saturation.
- **Outlier handling.**

Acceptance criterion: at calm-baseline (`s = v = σ = 0, q = 1`) the composite score must satisfy `S ≤ S₀`.

Owner placement: (N1) Oracle Integration Spec for the canonical path; (N2) FireRate-side fallback from snapshot history if Oracle does not deliver `(normalized)` signals in iteration 1.

### 6. Fallback functional forms for Step 2

The sigmoid form in §IV.1.2 is the recommended form per spec §3.5.3 and the GPT-vs-Claude analysis in `/research/[Research] Firerate Formula - discussion.md`. Two viable fallbacks are documented here in case iteration-2 calibration or onchain gas profiling rules out the sigmoid:

**Fallback 1 — Piecewise-linear (PWL).** Each stress component passes through a per-signal piecewise-linear penalty function `pwl_i: [0, ∞) → [0, 1]`. The four penalties are combined linearly with weights `w_i` and inverted:

```
total_penalty = w_s · pwl_s(stress_s) + w_v · pwl_v(stress_v)
              + w_σ · pwl_σ(stress_σ) + w_q · pwl_q(stress_q)
m = 1 − clamp(0, 1 − m_min, total_penalty)
```

with `stress_i = max(0, x̂_i − 1)` for the three above-baseline signals and `stress_q = max(0, 1 − q̂)` for volume. Trade-off: lowest gas cost (no `exp` required), trivially auditable (integer tables), trivially tunable via governance (replace a table). Less smooth than the sigmoid, but inter-window ramp limit absorbs any micro-roughness. Should be the first fallback if `sigmoid` gas cost is prohibitive on chain.

**Fallback 2 — Hill-function (generalized sigmoid) product.** Each signal gets a Hill response `f_i(stress_i) = 1 / (1 + (stress_i / x_half_i)^k_i)`; combine multiplicatively:

```
m = m_min + (1 − m_min) · f_s · f_v · f_σ · f_q
```

Trade-off: stresses compound multiplicatively, which may match panic dynamics better but is harder to reason about parameter-by-parameter. Should be considered if a future game-theory analysis shows that a single composite score `S` is insufficient and the protocol must require *simultaneous* manipulation of multiple independent signals to drive `m` down. This matches the §6 note in `/research/[Research] Firerate Formula - discussion.md` ("the multiplicative form can replace Step 2 while preserving Steps 1 and 3").

### 7. Conversion Rate as separate Oracle-driven output

A third option for `base_rate` beyond Models A and B: derive it from a *different* function of the same signals, distinct from `m(S)`. This would decouple capacity (`V_t`) and rate (`base_rate`) entirely. Not recommended in v1 (doubles the calibration surface for no obvious gain), but flagged here for completeness.

### 8. Pro-rata: unused allocation (lost vs carried)

The same question shows up in two places: (i) one-sided batch (e.g. `Q_G = 0`, `Q_P > 0`) — §IV.2.4 Scenario D states the allocation is not redistributed between token types but does not say what happens to it; (ii) general Model A with light demand — `coens_minted = Q · m(S) ≪ allocation` (see §IV.2.4 Scenario A: 70k emitted against 392k allocation). Under Model A this is the common case, not an edge case. Product decision required — same answer should cover both forms.

***

## Appendix A. Worked End-to-End Example

*(To be filled in.)*
