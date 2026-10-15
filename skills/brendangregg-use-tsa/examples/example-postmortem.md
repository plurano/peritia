# Example Postmortem (worked example of the template)

> A fictional but realistic incident, written with
> [`templates/postmortem-report.md`](../templates/postmortem-report.md) and
> showing how the USE + TSA trail appears in a finished document. Use it to
> calibrate depth: evidence rows are short, every claim traces to a command,
> and ruled-out hypotheses stay on the record.

---

## Postmortem: API p99 latency 20× regression caused by cgroup CPU throttling

| | |
| --- | --- |
| **Status** | Final |
| **Incident date(s)** | 2026-07-14 |
| **Author(s)** | D. Ibrahim (with AI agent assistant) |
| **System(s) affected** | `orders-api` (Go 1.24), container `orders-7f9c4d6b4-x2`, host `prod-node-03` (8 vCPU, Ubuntu 24.04, kernel 6.8) |
| **Severity** | SEV2 — degraded, not down: p99 latency 95ms → 1.9s for 47 minutes |

## 1. Summary

Between 14:03 and 14:50 UTC on 2026-07-14, `orders-api` p99 latency rose from
95ms to 1.9s. The service was being CPU-throttled by its cgroup limit after a
deploy raised the replica count on the node, oversubscribing CPU. TSA showed
61% of thread time in the Runnable state while system-wide CPU utilization was
only 48% — the signature of a resource-control limit, not a busy host.
Raising the container's CPU limit restored p99 to 110ms within 5 minutes.

## 2. Impact

- **User-facing effect**: checkout API p99 latency 95ms → 1.9s; 0.3% of
  requests timed out at the 2s gateway limit (~410 requests).

- **Duration**: 2026-07-14 14:03 UTC → 14:50 UTC (47 min).
- **Scope**: `orders-api` on `prod-node-03` only; other services on the node
  unaffected; no data loss.

## 3. Root Cause

- **Trigger**: 14:02 UTC deploy raised `orders-api` replicas on
  `prod-node-03` from 1 → 2, increasing node CPU demand.

- **Root cause**: the container's cgroup CPU limit (`cpu.max = 150000/100000`
  = 1.5 CPUs) throttled the process once real demand exceeded 1.5 CPUs. The
  limit had been sized for the old single-replica layout and was never
  revisited.

- **Contributing factors**: host-level monitoring showed only 48% CPU, so the
  node alert (fires at 80%) never fired; no alert existed on
  `cpu.stat.throttled` counters.

Causal chain: more replicas → higher per-container CPU demand → demand
exceeded the 1.5-CPU cgroup quota → CFS throttling → threads spent 61% of time
Runnable while *executing* normally when scheduled → request latency inflated
~20×.

## 4. Detection

Grafana p99 panel watch by on-call at 14:09 UTC (human, not alert — 6 minutes
after onset). No automated alert covered per-container throttling.

## 5. Investigation Log

| # | Time (UTC) | Action / command | Hypothesis | Result | Conclusion |
| --- | --- | --- | --- | --- | --- |
| 1 | 14:11 | Problem statement questions with on-call | Recent change? | Deploy at 14:02 raised replicas | Investigate deploy correlation |
| 2 | 14:14 | 60s sweep: `uptime`, `dmesg \| tail`, `vmstat 1`, `mpstat -P ALL 1` | Host CPU/mem/disk issue | load 5.1/8; no OOM; `r`=3, us+sy=48%; no iowait | Host resources **exonerated** |
| 3 | 14:16 | `iostat -xz 1`, `sar -n DEV 1`, `sar -n TCP,ETCP 1` | Disk/network bottleneck | `%util` 12%, await 0.8ms; retrans 0/s | Disk, network **exonerated** |
| 4 | 14:19 | TSA: `pidstat 1 -t -p <pid>` + `/proc/<pid>/task/*/schedstat` run_delay | Where do threads wait? | run_delay ≈ 61% of elapsed; %CPU capped at 150% | Runnable dominates → check CPU controls |
| 5 | 14:22 | `cat /sys/fs/cgroup/.../cpu.max`; `cat .../cpu.stat` | Cgroup throttling | `cpu.max 150000 100000`; `nr_throttled` rising ~900/s | **Confirmed** |
| 6 | 14:24 | `kubectl top pod` + request-rate panel | Demand vs quota | 2.9 CPUs demanded vs 1.5 allowed | Explains throttle onset at deploy |

**Methodologies used**: Problem Statement → 60s triage (USE subset) → TSA →
resource-control check. **Exonerated**: host CPU (row 2), memory (row 2),
disk (row 3), network (row 3), application deploy bug (row 1 — the deploy was
the trigger, not a code regression).

## 6. Evidence Table

| Claim | Command | Output (key fields) | Interpretation |
| --- | --- | --- | --- |
| Host not saturated | `vmstat 1` | r=3, us 31, sy 17, id 52, wa 0 | 8 CPUs half idle — not a host CPU problem |
| Threads wait to run | `/proc/<pid>/task/*/schedstat` | run_delay sum 61% of wall time over 60s | Runnable state dominates (TSA) |
| Quota is the limiter | `cat cpu.max` / `cpu.stat` | 150000/100000; nr_throttled +54k/min | CFS throttles at 1.5 CPUs |
| Demand exceeds quota | `kubectl top pod` | 2.9 CPUs at peak | Throttling inevitable under new replica layout |

## 7. Resolution

- **Immediate mitigation**: 14:45 UTC — raised container limit to 4 CPUs
  (`cpu.max 400000 100000`). p99 fell to 110ms by 14:50 UTC.

- **Permanent fix**: 2026-07-16 — limits/requests regenerated from actual
  usage for all services on the node; replica-count changes now require a
  quota review checkbox in the deploy template.

- **Verification**: `cpu.stat nr_throttled` flat at 0/s for 72h post-fix; p99
  95–120ms under a 2026-07-17 load test at 1.4× peak traffic (same
  measurement path as row 6).

## 8. What Went Well / What Went Poorly

- **Well**: triage exonerated the host in 5 minutes; TSA pointed at Runnable
  before any code was read; no random restarts.

- **Poorly**: detection was human, not alert; quota sizing was tribal
  knowledge; the 14:02 deploy did not flag quota impact.

## 9. Prevention / Follow-up

| Action | Type | Owner | Due | Status |
| --- | --- | --- | --- | --- |
| Alert on `cpu.stat.throttled` rate > 0 for 5m | detect | SRE | 2026-07-21 | Done |
| Quota review step in deploy template | prevent | Platform | 2026-07-16 | Done |
| Runbook: "Runnable >10% → check cgroup CPU limits first" | document | SRE | 2026-07-18 | Done |
| Audit other services' `cpu.max` vs p95 usage | mitigate | Platform | 2026-07-31 | Open |

## 10. Lessons & Notes for Next Time

- Host-level CPU% says nothing about container throttling. Always check
  `cpu.stat` when Runnable dominates on a containerized host.

- Known-unknown surfaced: per-container *memory* throttling counters were not
  collected either; audit scheduled (row above covers CPU only).
