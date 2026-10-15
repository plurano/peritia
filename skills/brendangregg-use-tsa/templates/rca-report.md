# Root Cause Analysis (RCA) Report Template

> Use for the analysis itself — the working document that may later feed a
> postmortem. Lighter than a postmortem, heavier on hypotheses and evidence.

## RCA: <problem statement in one sentence>

| | |
| --- | --- |
| **Date opened** | YYYY-MM-DD |
| **Analyst** | <name / agent> |
| **System** | <host/service/version/environment> |
| **Status** | Investigating / Root cause found / Verified / Closed |

## 1. Problem Statement

(Answer the Problem Statement Method questions)

- What makes you think there is a performance problem?
- Has this system ever performed well?
- What changed recently? (software / hardware / load / configuration)
- Can the degradation be expressed as latency or run time? Quantify:
  <before vs after, p50/p99, throughput>

- Who/what is affected? Only you, or others too?
- Environment: OS, kernel, versions, configuration, hardware/VM/container
  limits.

## 2. Current Understanding (one paragraph)

The best current explanation of the mechanism, updated as you learn. Mark
confidence: `hypothesis` / `supported` / `confirmed`.

## 3. Hypotheses

| # | Hypothesis | Status (open / ruled out / confirmed) | Test used | Evidence |
| --- | --- | --- | --- | --- |
| H1 | | | | |
| H2 | | | | |

Rule: every hypothesis gets a **falsifiable** test. Record ruled-out
hypotheses — they narrow the search space (Process of Elimination).

## 4. Measurements Taken

| Time (UTC) | Command / method | Key output | Supports/refutes |
| --- | --- | --- | --- |
| | | | |

## 5. Methodology Trail

Which methods were applied, in order, and what each concluded:
<e.g. 60s triage (no errors, CPU saturated) → USE (CPU util 100%, run-queue
9) → TSA (80% Runnable) → resource control found: cgroup CPU limit>

## 6. Root Cause

<The mechanism, stated precisely enough that a fix follows from it.>

Causal chain: A → B → C → observed symptom.

## 7. Recommended Actions

1. Immediate mitigation: <...>
2. Permanent fix: <...>
3. Detection improvement: <which metric/alert would have caught this earlier>
4. Related hardening: <same class of issue elsewhere>

## 8. Verification Plan

How you will prove the fix: the measurement, the expected before/after, and
the window of observation. Distinguish "fix applied" from "fix verified".
