# [Validation] Phase 1 Findings

| **Owner** | Lev Menshchikov |
| **Status** | Draft v0.1 |
| **Source of truth** | `[Specification] Firerate.md` |
| **Documents validated** | `/workspace/[RU] Firerate Formulas.md`, `/workspace/[RU] [#1] Briefing.md`, `/artifacts/[Research] Firerate Formulas.md` |
| **Scope** | Substantive errors and material inconsistencies. Minor stylistic issues out of scope by user request. |

***

## TL;DR

Phase 1 research is structurally solid and faithful to the spec on the macro level: the three steps that mirror §3.5.3 are reproduced correctly, the pro-rata formalization is mathematically clean, the per-token model split (A vs B) is well-framed, and the open questions catalogue is comprehensive. Arithmetic in the worked examples checks out step by step.

However, four substantive issues need attention before phase 2:

- **B1 — Normalization scheme and worked-example values are mutually inconsistent.** The `x̂ = x / x_baseline` definition (median-anchored, `x̂ = 1` means "at long-run normal") contradicts the example signal values, which behave as if normalization were min-max to `[0, ~1.5]` with higher = worse. Under the stated scheme, the parameter set produces `S > S₀` even at fully normal conditions.
- **B2 — Settlement time-indexing diverges between the English artifact and the new RU version, without an explicit changelog.** The artifact says signals from window `t−1` inform decisions at window `t`; the RU version chose aggregation over the current window `t`. Both are defensible, but the documents now disagree, and the cool-down argument in §3.6 of the spec only holds under specific readings.
- **B3 — `m_min` and `V_min` interact silently.** Two distinct floors are defined without a stated relationship; only one of them ever binds, and the research does not surface this constraint for calibration.
- **B4 — Cold-start anchor for the ramp creates a structural emission ceiling tied to the activation moment.** Using `V_{t−1} := V_base · m(S(t_activate))` with `S(t_activate) ≈ S₀` traps the protocol at roughly `V_base · 0.5` for many windows after a brief stress spike subsides, even if the spike was a manipulation.

The remainder of the document details each finding with location, evidence, and impact. A short list of medium-severity observations follows, with cosmetic items omitted by request.

***

## A. Cross-check baseline

What is correct and verified, so the rest of the report can focus on what is not.

The three-step `V_t` derivation in `[RU] Firerate Formulas.md` §V.1 reproduces spec §3.5.3 verbatim in symbol and structure: composite stress score with `clamp(S_raw, 0, 1)`, inverted-sigmoid stress multiplier anchored to `V_base`, asymmetric ramp limit against `V_{t−1}`. Monotonicity argument (§V.1.4) is sound — strict monotonicity in the unclamped interior, non-strict everywhere else.

The pro-rata mathematization in §V.2.2 is the cleanest part of the document. The two-argument `min(base_rate, allocation/Q)` form is the correct algebraic expression of "scaled down proportionally so that total coens mined does not exceed the allocation" from spec §3.4. Settlement invariants in §V.2.5 follow directly.

Worked-example arithmetic in §V.1.5 and §V.2.4 is internally consistent. I rechecked all three `V_t` cases and all four pro-rata scenarios; every step matches.

Open Questions section §IX is comprehensive: it routes each unresolved item to an owner (quant calibration, product decision A vs B, Oracle Integration Spec for `s`-publication and normalization scheme).

The Briefing document `[RU] [#1] Briefing.md` is a faithful summary of the formulas document and adds correct framing of the AI-assisted derivation history (`/research/[Research] Firerate Formula - discussion.md`).

***

## B. Substantive findings

### B1. Normalization scheme contradicts worked examples

**Location.** `[RU] Firerate Formulas.md` §III.2 (definition) vs §V.1.5 (worked examples). Same contradiction is mirrored in `[Research] Firerate Formulas.md` §III.1 vs §IV.1.5.

**The definition.** Section III.2 states:

> $\hat{x} = \dfrac{x}{x_\text{baseline}}$, где $x_\text{baseline}$ — скользящий референс по многодневному окну (например, 30-дневная медиана). Нормализованное значение $\hat{x} = 1.0$ означает «текущее условие равно долгосрочной норме», $\hat{x} = 2.0$ — «вдвое выше нормы».

Under this scheme, `x̂ = 1` is the normal-conditions reference and `x̂ → 0` is "much better than normal" (for stress signals) or "much less than normal" (for volume, which is bad).

**The examples.** Case 3 ("severe stress") in §V.1.5:

| Signal | Used value | Implied meaning under §III.2 |
| --- | --- | --- |
| `s` | 1.50 | spread 1.5× the 30-day median — modestly elevated |
| `v` | 1.00 | velocity *at* baseline |
| `σ` | 0.80 | volatility *below* baseline by 20% |
| `q` | 0.10 | volume at 10% of baseline (this one fits) |

This is not "severe stress". A real stress event (think March 2020 in crypto) has spreads 5–10× normal, volatility 3–5× normal, velocity well above 1.0. The example uses values clustered around `[0.1, 1.5]`, which suggests the implicit normalization is something closer to min-max scaling to `[0, ~1.5]` with 1 = "stressed extreme", not the median-anchored scheme stated in §III.2.

**Why this matters for the parameter set.** Take the stated weights `(w_s, w_v, w_σ, w_q) = (0.40, 0.25, 0.20, 0.15)`, sum to 1.0, and evaluate at the **defined** baseline (`s = v = σ = q = 1.0`):

```
S_raw = 0.40·1 + 0.25·1 + 0.20·1 − 0.15·1 = 0.70
S     = clamp(0.70, 0, 1) = 0.70
```

With `S₀ = 0.40` and `k = 10`, this gives `m(S) ≈ 0.142` and `V_target ≈ 0.142 · V_base`. So **at long-run normal conditions the protocol thinks it is in severe stress**. The activation threshold (`S₀ = 0.40`) is below the stress score at baseline, which would mean FireRate is permanently Active.

This is not a bug in any single line — it is a calibration inconsistency between the three things that have to fit together: normalization scheme, weights, and `S₀`. The phase-1 document presents all three as independent placeholders, but they are coupled. Whoever calibrates `S₀` later will discover that with `x/baseline` normalization, `S₀` needs to be either much higher than `0.40`, or weights need to be much smaller, or the normalization needs to subtract the baseline (z-score style) rather than divide by it.

**Impact.** Without resolving this, the calibration exercise in phase 2 will start from a parameter set that no consistent normalization can support. The "open question" framing of `S₀` and weights does not make this go away — these are not independently tunable.

**Suggested action.**
- Pick a normalization scheme that makes `x̂ = 0` correspond to "calm" and `x̂ = 1` correspond to "the level at which FireRate should engage" (so `S₀` is interpretable). This is min-max or excess-over-baseline, not pure `x/baseline`.
- Re-run the worked examples after the choice is made, so the numeric scenarios match the definition.
- Move this from §IX.3 ("schema is open") to §IX.2 ("blocking for calibration") and add explicit acceptance criterion: at long-run normal conditions, `S ≤ S₀`.

***

### B2. Settlement time-indexing differs between artifact and RU version

**Location.** `[Research] Firerate Formulas.md` §III.3 vs `[RU] Firerate Formulas.md` §III.4.

**The disagreement.**

Original artifact (`§III.3 Notation Conventions`):

> A Batch Window is indexed by `t`; signals evaluated for window `t` are sampled over the closed-out preceding window (i.e. `t−1` data informs `t` decisions). This avoids in-window manipulation feedback loops.

New RU formulas (`§III.4`):

> Мы принимаем прочтение (b) — это единственная интерпретация, при которой §3.6 спеки имеет смысл: §3.6 обосновывает защиту тем, что атакующему надо удерживать манипуляцию на бирже долго, а это работает только если сигналы агрегируются за весь signal-sampling window.

Reading (b) is "signals aggregated over the current window `t`, minutes 0–60". This is materially different from "signals from `t−1` inform `t`". The RU version makes the new choice without acknowledging that the prior document made a different one.

**Why both versions are partly right and partly wrong.**

Under "previous-window" reading (artifact):
- No feedback loop between current tickets and current rate. Clean separation.
- But §3.6 spec talks about a *signal-sampling window* against which the attacker must commit. If signals are sampled over `t−1`, then the attacker who wants to depress the rate for the batch that settles at end of `t` must commit during `t−1`. This still constitutes a commitment window, just shifted backward. §3.6 is not broken under this reading.

Under "current-window" reading (RU):
- The cool-down (minutes 30–60) does lock the *ticket composition*, but signals from minutes 30–60 still affect the rate. So the feedback loop is only partially broken — an attacker can manipulate in minutes 30–60 to depress the rate at which the already-locked tickets settle.
- The attacker's own tickets are also locked, so they cannot extract differential value (this matches §3.6 spec bullet 3, "all participants settle at the same rate"). But they can still suppress rate generally to harm other holders — useful if the attacker is short coen on an exchange.

**The "(b) is the only reading that makes §3.6 work" claim in the RU version is overstated.** The artifact's "previous-window" interpretation also gives §3.6 a coherent meaning — it just shifts the commitment window one slot earlier in time.

**Impact.**
- The two documents now contradict each other on a substantive design point.
- The choice has real downstream consequences for the cost-of-attack analysis in §VI (deferred to next iteration). Under (b), the cost-of-attack formula must integrate over minutes 30–60 separately from minutes 0–30. Under "previous-window", the integration is uniform over a single past window.
- Whoever picks up the cost-of-attack work will inherit ambiguity unless the choice is documented and the rationale tightened.

**Suggested action.**
- Either bring the artifact in line with the RU version (make (b) canonical, drop the "previous-window" sentence), or vice versa.
- Replace the "only reading that makes §3.6 work" justification with the real trade-off: (b) preserves a longer signal-sampling window at the cost of in-window feedback in minutes 30–60; "previous-window" eliminates in-window feedback but shifts the attack window.
- Add an explicit note that under (b) the cool-down protects *ticket composition* but not *signal contribution*.

***

### B3. `m_min` and `V_min` are silently redundant

**Location.** `[RU] Firerate Formulas.md` §II.2 (parameter list), §V.1.2 (Step 2 formula), §IX.1 (calibration open questions).

**The two floors.** The model defines two independent lower bounds on `V_target`:

- `m_min` — minimum stress multiplier, gives `V_target ≥ V_base · m_min` from the structure of the sigmoid.
- `V_min` — hard absolute floor, applied by `clamp(V_base · m(S), V_min, V_max)`.

**The interaction.** `V_target = clamp(V_base · m(S), V_min, V_max)`. The clamp's lower bound binds only when `V_base · m(S) < V_min`, i.e. when `m(S) < V_min / V_base`. Since `m(S) ≥ m_min`, the clamp binds only if `m_min < V_min / V_base`, i.e. `V_base · m_min < V_min`.

Two regimes:

| Relationship | What binds | Consequence |
| --- | --- | --- |
| `V_min ≤ V_base · m_min` | `m_min` always binds first | `V_min` is dead parameter — never triggers |
| `V_min > V_base · m_min` | `V_min` always binds first | `m_min` is partially dead — `V_target` never reaches `V_base · m_min` |

In both cases, one of the two is silently inert. The two parameters do not stack as independent safety nets — they are alternatives, and the calibrator who tunes them independently will inevitably set one to be dominated by the other.

The research does not flag this. §IX.1 lists `m_min` and `V_min` as two separate calibration items as if they were independent levers.

**Impact.** Phase 2 calibration will produce one of:
- Either `V_min ≈ V_base · m_min` set intentionally to make both binding at the same threshold (in which case one is redundant by construction), or
- An accidental dominance where governance thinks they have two safety floors but actually have one.

**Suggested action.**
- Add a one-paragraph note in §V.1.2 explaining the interaction and stating the constraint, e.g. `V_min` is intended as a configuration-error guard (against `m_min` set too low), so the recommended relationship is `V_min < V_base · m_min`.
- In §IX.1, mark `V_min` and `m_min` as a *paired* calibration: their relationship matters more than either absolute value.

***

### B4. Cold-start anchor traps the protocol near `0.5 · V_base`

**Location.** `[RU] Firerate Formulas.md` §V.1.3 ("Для первой активации Firerate $V_{t-1}$ не определён...").

**The formula.** For the very first window after activation:

```
V_{t-1} := V_base · m(S(t_activate))
```

**The problem.** The activation moment `t_activate` is the moment `S` crosses the activation threshold (which per §III.5 is aligned with `S₀`). At that moment, `m(S₀) = (1 + m_min) / 2`. For the placeholder `m_min = 0.10`, this is `0.55`. So:

```
V_{t-1} ≈ 0.55 · V_base
```

The asymmetric ramp limit then constrains all subsequent windows:

```
V_t ∈ [V_{t-1} · (1 − r_down), V_{t-1} · (1 + r_up)]
    ≈ [0.385 · V_base, 0.605 · V_base]   for r_down=0.30, r_up=0.10
```

If the stress was a brief spike (one window) and immediately subsided so that for window 2 we have `S ≈ 0` and `V_target ≈ V_base`:

```
V_2 = clamp(V_base, 0.385·V_base, 0.605·V_base) = 0.605·V_base
V_3 = clamp(V_base, 0.605·V_base · 0.7, 0.605·V_base · 1.1) = 0.666·V_base
V_4 ≈ 0.732·V_base
...
```

Approaching `V_base` from `0.55 · V_base` at `r_up = 10%` per window takes ~7 windows. During those 7 hours, the protocol underemits — by construction, defensively. This is fine if the activation was a real stress event. It is *not* fine if the activation was triggered by a manipulator who briefly inflated the spread.

**Why this matters.** The asymmetric ramp `r_down > r_up` is justified in §V.1.3 precisely as a defence against this attack — "даже если атакующий ненадолго раздул spread, а потом убрал ордера, $V_t$ ползёт вверх только с медленным $r_\text{up}$". But that argument assumes the protocol was running with `V_{t-1}` at near-`V_base` *before* the attack. On a cold-start, the anchor is already at `~0.55 · V_base`, so the attacker only needs to *trigger activation* to get the slow-relaxation regime. The friction is the same whether the attack was severe or barely-over-threshold.

This is one of the three options laid out for **re-activation** in §V.1.3 (option (a) cold-start), but it is presented unconditionally as the cold-start choice for *first* activation. The re-activation discussion lists three options and defers; first-activation is treated as settled. The two should be analyzed together — they are the same question with the same trade-offs.

**Impact.**
- The cold-start choice creates a free leverage point for an attacker who can pay the cost of triggering activation once.
- The cold-start and re-activation decisions are presented inconsistently (one settled, one open).
- Phase-2 cost-of-attack analysis in §VI will need to account for this.

**Suggested action.**
- Move the cold-start choice from a settled decision in §V.1.3 to an open question, alongside the re-activation options (a/b/c).
- Add explicit option (d): "cold-start anchored at `V_base` directly, accepting one window of fast tightening if stress is real". This is more aggressive but removes the activation-as-attack vector.
- Tie the choice to the cost-of-attack analysis: the right cold-start depends on whether the attacker can profitably trigger activation just to lock the protocol at half capacity.

***

## C. Medium-severity observations

These are real but second-order. Listed for completeness; do not block phase 2.

**C1 — "Severe stress" example does not produce severe behaviour.** Case 3 in §V.1.5 has the worst signals in the example set, yet `V_t = 560,000` (same as Case 2). The ramp cap absorbs the entire difference. The research notes this, but the documented behaviour does not match the label — readers see "severe stress" produce "same throttle as mild stress". A fourth example showing the protocol after 2–3 sustained severe-stress windows (where ramp has fully bitten) would close this gap.

**C2 — Model A leaves emission room unused without explicit treatment.** Under Model A with `base_rate = m(S) = 0.351` and a light batch (`Q_G · base_rate = 70,200`), the protocol emits 70,200 coen against `allocation_G = 392,000`. The open question "lost vs carried allocation" in §IX.2 covers the *Promis-only* edge case but not the *general Model-A underrun*, which is the common case under Model A. The two are the same question; flag together.

**C3 — Model A vs Model B asymmetry in attack surface is asserted but not derived.** §V.2.1 says "под Моделью B атакующему выгоднее раздувать капу через oversubscription, под A — подавлять $m(S)$ напрямую". This is plausible but unsupported. Under Model A, an attacker who can suppress `m(S)` gets *double* leverage (both `V_t` and `base_rate` go down). Under Model B, the only attack vector is supply-side flooding — which itself requires the attacker to commit large amounts of source token. The directional claim in §V.2.1 may understate Model A's exposure. Worth a paragraph in §VI when it is filled in.

**C4 — Spec mentions "Promis from Emit", no Emit spec exists.** FireRate spec §3.1 says "Gratis from Nod, Promis from Intex, Promis from Emit". `[Specification] Promis.md` and `[Specification] Intex.md` both describe only the Intex → Promis path. The Glossary mentions "Promis from Emit" in the Mining Module entry but has no Emit term. This is a spec-level gap, not a research-level error, but FireRate's scope statement ("All other Mining Module flows … are unaffected") implicitly assumes the Emit path exists. Worth flagging back to the spec owner. The phase-1 research does not catch this.

**C5 — English artifact is stale relative to RU version.** `[Research] Firerate Formulas.md` §V/VI/VII are `*(To be filled in.)*` placeholders; the RU version `[RU] Firerate Formulas.md` has §VI/VII/VIII as "Запланировано на следующую итерацию" plus a complete acceptance-criteria draft in §VIII (8 invariants) and additional content in §III (normalization fallback, time-indexing, activation assumptions) and §V.1.3 (re-activation). The English artifact is the deliverable per `readme.md` (`/artifacts/[Research] Firerate Formulas.md`). It needs to be brought into sync before phase 2, or the deliverable misrepresents the work done.

***

## D. What is not in scope of this validation

By the user's request, the following were inspected but not formally reviewed:

- Stylistic and wording issues.
- Minor inconsistencies in symbol formatting (e.g. `V_{t-1}` vs `V_{t−1}`).
- Diagram / flow visualization (§VII placeholder).
- Cost-of-attack derivation (§VI placeholder — entirely deferred).

If a deeper review of any of these is wanted, flag separately.

***

## E. Recommended action ordering

Before phase 2 begins:

1. Resolve B1 (normalization scheme) — blocks any meaningful parameter calibration.
2. Reconcile B2 (settlement time-indexing) — pick one reading across both documents and add a sentence on the trade-off.
3. Add the relationship constraint between `m_min` and `V_min` (B3) — single paragraph in §V.1.2.
4. Reopen the cold-start choice (B4) and merge with re-activation analysis in §IX.2.
5. Sync the English artifact to the RU version (C5) — administrative but the artifact is the canonical deliverable.

C1–C4 can wait until phase 2 work on §VI (cost-of-attack) starts; some will resolve as a side effect of that work.

## F. Open questions raised by this validation

- Is there a stated reason the settlement time-indexing changed between the artifact (`t−1` informs `t`) and the RU version (aggregation over `t`)? If yes, capture it; if no, the change needs a brief decision note.
- Was the cold-start formula `V_{t-1} := V_base · m(S(t_activate))` chosen before or after the asymmetric ramp justification in §V.1.3? The two arguments cut against each other and need to be reconciled.
- Under the chosen normalization scheme (whatever it becomes after B1), do the existing worked-example signal values still illustrate "calm / mild / severe"? If not, regenerate examples.
