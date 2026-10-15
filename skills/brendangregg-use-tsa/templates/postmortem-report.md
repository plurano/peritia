# Postmortem Report Template

> Fill every section. If a section does not apply, write "N/A — <reason>"
> rather than deleting it. Use **absolute dates** everywhere (ISO 8601), never
> "yesterday" or "last Tuesday". Be candid: a postmortem that protects egos
> protects the failure too.

## Postmortem: <short, specific title — name the mechanism, not the symptom>

| | |
| --- | --- |
| **Status** | Draft / Final |
| **Incident date(s)** | YYYY-MM-DD [– YYYY-MM-DD] |
| **Author(s)** | <name / agent + supervising human> |
| **System(s) affected** | <hosts, services, versions> |
| **Severity** | <SEV level + one-line justification> |

## 1. Summary

Two to four sentences: what broke, what the root cause turned out to be, and
what fixed it. Write this last. A reader who stops here should still know the
essentials.

## 2. Impact

- **User-facing effect**: <latency, errors, outage, data loss; quantified>
- **Duration**: <start → end, absolute timestamps>
- **Scope**: <which services/tenants/regions; what was NOT affected>
- **Business/material impact**: <if known>

## 3. Root Cause

State the single mechanism that, if removed, prevents recurrence. Distinguish:

- **Trigger** — what initiated the incident (the change, the load spike, ...)
- **Root cause** — the condition that made the trigger harmful
- **Contributing factors** — what made it worse or slower to detect/fix

Include the causal chain as evidence-backed steps: *A caused B, which caused
C, observed as symptom D.* Mark any step you could not verify with
`(unverified)` — do not dress a guess up as fact.

## 4. Detection

- How was the issue noticed? <alert, user report, dashboard, accident>
- Time from onset to detection: <duration> — and why that long/short.

## 5. Investigation Log (methodology trace)

Record the path taken, including dead ends — future readers need to know what
was **ruled out**, not only what was found.

| # | Time (UTC) | Action / command | Hypothesis | Result | Conclusion |
| --- | --- | --- | --- | --- | --- |
| 1 | | | | | |
| 2 | | | | | |

**Methodologies used**: <e.g. Problem Statement → USE sweep → TSA on hot
process → off-CPU analysis → 5 Whys>

**Resources exonerated** (with evidence): <CPU util 22%, no saturation — see
row 4; network retrans 0/s — see row 7; ...>

## 6. Evidence Table

Every claim in §3 must trace to a row here.

| Claim | Command / measurement | Output (key fields) | Interpretation |
| --- | --- | --- | --- |
| | | | |

## 7. Resolution

- **Immediate mitigation** (what restored service, when)
- **Permanent fix** (what removed the root cause, when)
- **Verification**: how the fix was proven — before/after metrics, same
  measurement as the evidence table. "Deployed" is not "verified."

## 8. What Went Well / What Went Poorly

- Well: <fast detection, clean rollback, ...>
- Poorly: <missing telemetry, runbook gap, slow handoff, ...>

## 9. Prevention / Follow-up

| Action | Type (prevent / detect / mitigate / document) | Owner | Due | Status |
| --- | --- | --- | --- | --- |
| | | | | |

Consider: monitoring/alerting on the saturation or error metric that would
have caught this in minutes; eliminating the class of problem (not just this
instance); runbook/checklist updates.

## 10. Lessons & Notes for Next Time

Anything that doesn't fit above: environment quirks, tool gaps discovered,
"known-unknowns" the investigation exposed (metrics you could not obtain and
why).

---

*Template from the brendangregg-use-tsa skill. Methodologies:
[USE](../references/use-method.md) ·
[TSA](../references/tsa-method.md) ·
[toolbox](../references/methodology-toolbox.md).*
