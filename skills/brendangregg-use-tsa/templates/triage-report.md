# Triage Report Template (First Response)

> The 5–15 minute record. Goal: decide *where* to dig next and document what
> has been exonerated — not to find root cause yet. Attach or reference raw
> command output; don't paste walls of it.

## Triage: <host / service> — YYYY-MM-DD HH:MM UTC

**Analyst**: <name / agent>  **Trigger**: <alert / report / routine>

## 1. Symptom (as reported)

<One or two sentences, with the reporter's own numbers if any.>

## 2. First-60-Seconds Sweep (Linux)

| Check | Command | Result (key fields) | Verdict (OK / suspect / exonerated) |
| --- | --- | --- | --- |
| Load trend | `uptime` | | |
| Kernel errors | `dmesg \| tail` | | |
| CPU saturation, swap, iowait | `vmstat 1` | | |
| Per-CPU balance | `mpstat -P ALL 1` | | |
| Per-process CPU | `pidstat 1` | | |
| Disk I/O | `iostat -xz 1` | | |
| Memory | `free -m` | | |
| NIC throughput | `sar -n DEV 1` | | |
| TCP health | `sar -n TCP,ETCP 1` | | |
| Variable load | `top` | | |

## 3. USE Sweep Notes

Resources checked beyond the 60s list (controllers, interconnects, software
resources, cgroup/hypervisor limits) and anything marked "?" (unmeasurable —
a known-unknown):

| Resource | Utilization | Saturation | Errors | Verdict |
| --- | --- | --- | --- | --- |
| | | | | |

## 4. TSA Notes (if a specific application is implicated)

Threads of interest: <PID/comm>. Dominant states observed and evidence:
<e.g. 55% Runnable via runqlat+pidstat; 30% Sleeping on disk I/O via
offcputime stacks>

## 5. Triage Conclusion

- **Primary suspect**: <resource / state / component> because <evidence>
- **Exonerated so far**: <list with one-line evidence each>
- **Known-unknowns**: <what could not be measured and why>
- **Next methodology to apply**: <e.g. off-CPU analysis with `offcputime`,
  drill-down into storage layer, workload characterization of top talker>

- **Immediate risk/mitigation**: <anything to do NOW — throttle, fail over,
  roll back>
