# The USE Method (Utilization, Saturation, Errors)

> Distilled from Brendan Gregg, "The USE Method"
> (<https://www.brendangregg.com/usemethod.html>) and "Thinking Methodically
> about Performance" (ACM Queue 2012 / CACM 2013). Read this when you need the
> full method; `SKILL.md` only carries the operating procedure.

## One-line summary

**For every resource, check utilization, saturation, and errors.**

The USE Method is a resource-oriented methodology for analyzing the performance
of any system. It directs the construction of a checklist that quickly
identifies resource bottlenecks and errors. It begins by posing questions and
then seeks answers — instead of beginning with whatever metrics happen to be
available (partial answers) and trying to work backwards.

Think of it as the emergency checklist in a flight manual: simple,
straightforward, complete, and fast. It is intended to be used**early** in an
investigation to identify *systemic* bottlenecks, and it finds roughly 80% of
server issues with about 5% of the effort. It is one tool in a larger toolbox —
latency-based methodologies (e.g. Method R) can approach 100% of issues but
usually cost far more time.

## Terminology

| Term | Definition | Typical expression |
| --- | --- | --- |
| **resource** | All physical server functional components (CPUs, disks, busses, ...). Some *software* resources and imposed limits (resource controls) can also be treated this way. | — |
| **utilization** | The average time the resource was busy servicing work. | Percent over a time interval: "one disk is running at 90% utilization". |
| **saturation** | The degree to which the resource has extra work it cannot service, often queued. | Queue length: "the CPUs have an average run queue length of four". |
| **errors** | The count of error events. | Scalar counts: "this network interface has had fifty late collisions". |

Notes:

- A second definition of utilization exists — the *proportion of a resource
  that is used* — under which 100% means no more work can be accepted. Know
  which definition your metric uses.

- Errors matter even when the failure mode is recoverable: failed-and-retried
  operations and failed members of redundant pools degrade performance quietly.

- **Low average utilization does not rule out saturation.** Bursts of 100%
  utilization lasting seconds can hide inside a 5-minute average of 80%. Check
  the interval your monitoring averages over.

## The resource list

Start from a list of resources to iterate through. The generic server list:

- **CPUs**: sockets, cores, hardware threads (virtual CPUs)
- **Memory**: capacity
- **Network interfaces**
- **Storage devices**: I/O (request resource) *and* capacity — both can bottleneck
- **Controllers**: storage, network cards
- **Interconnects**: CPU, memory, I/O

Better than a list: a **functional block diagram** of the system, which also
shows relationships and data flow. Annotate each bus with its maximum bandwidth
and systemic bottlenecks may be visible before a single measurement is taken.

Interconnects are often overlooked. They are rarely the bottleneck, but when
they are, remediation is hard (upgrade the main board, or reduce load, e.g.
"zero copy" work to lighten the memory bus). USE at least makes you *aware* of
what you did not check.

Deliberately left out: hardware caches (MMU TLB/TSB, CPU caches). Caches
*improve* performance under high utilization; USE targets resources that
*degrade* under high utilization or saturation. Check cache hit rates after
systemic bottlenecks are ruled out. If unsure whether to include a resource,
include it and see how well the metrics work.

## The metric table (generic)

For each resource × metric type, fill in a metric and how to fetch it (tool +
statistic). Write "?" where none is available — you now have a checklist that
is as complete as your system allows, with the gaps documented as
known-unknowns.

Easy/common rows:

| Resource | Type | Metric |
| --- | --- | --- |
| CPU | utilization | CPU utilization (per-CPU or system-wide average) |
| CPU | saturation | Run-queue length or scheduler latency |
| Memory capacity | utilization | Available free memory (system-wide) |
| Memory capacity | saturation | Anonymous paging or thread swapping (maybe "page scanning") |
| Network interface | utilization | RX/TX throughput / max bandwidth |
| Storage device I/O | utilization | Device busy percent |
| Storage device I/O | saturation | Wait queue length |
| Storage device I/O | errors | Device errors ("soft", "hard", ...) |

Harder rows (often need custom tooling / CPU performance counters):

| Resource | Type | Metric |
| --- | --- | --- |
| CPU | errors | e.g. correctable CPU cache ECC events, faulted CPUs (if OS+HW support) |
| Memory capacity | errors | e.g. failed malloc()s (usually virtual memory exhaustion, not physical) |
| Network | saturation | Saturation-related NIC/OS events, e.g. "dropped", "overruns" |
| Storage controller | utilization | Controller max IOPS or throughput vs current activity |
| CPU interconnect | utilization | Per-port throughput / max bandwidth (CPU performance counters) |
| Memory interconnect | saturation | Memory stall cycles, high CPI (CPU performance counters) |
| I/O interconnect | utilization | Bus throughput / max bandwidth (e.g. Intel "uncore" events) |

A complete table runs to roughly thirty metrics. Fortunately the most common
issues are found with the easy rows — CPU saturation, memory capacity
saturation, network interface utilization, disk utilization — so check those
first.

## Software resources

Smaller software components can be treated the same way (not entire
applications):

- **Mutex locks**: utilization = time the lock was held; saturation = threads
  queued waiting on it.

- **Thread pools**: utilization = time threads were busy processing work;
  saturation = requests queued for the pool.

- **Process/thread capacity**: utilization = current usage vs the limit;
  saturation = waiting on allocation; errors = failed allocation ("cannot
  fork").

- **File descriptor capacity**: same pattern for FDs.

Do not sweat this type — if the metrics work well, use them; otherwise leave
software internals to other methodologies (e.g. latency analysis).

**Cloud/tenancy note:** hypervisor or container (cgroup) limits for CPU,
memory, network, and storage I/O are themselves resources. Examine each imposed
limit with USE just like a physical resource: "memory capacity utilization"
becomes tenant usage vs its cap; "saturation" can be anonymous paging even
while the traditional page scanner is idle.

## Suggested interpretations

- **Utilization**: 100% is usually a bottleneck (confirm with saturation and
  its effect). Past ~70% can already be a problem, because (a) long measurement
  intervals hide short 100% bursts, and (b) non-preemptible resources such as
  hard disks suffer noticeable queueing delays beyond that point — unlike CPUs,
  which can be preempted almost any time.

- **Saturation**: *any* non-zero degree can be a problem — queue length or time
  spent queued.

- **Errors**: non-zero counters are worth investigating, especially if still
  increasing while performance is poor.

- **The negative case is valuable**: low utilization, no saturation, no errors
  rapidly narrows the investigation's scope. Ruling areas out *is* progress.

## Strategy

1. Enumerate resources (list or functional block diagram).
2. For each resource, check errors first (a minor optimization — they are
   usually quicker and easier to interpret), then utilization, then saturation.

3. When a bottleneck is found, remember the system may suffer **more than one
   problem**: the first one you find may be *a* problem but not *the* problem.

4. Follow up each discovery with workload characterization and drill-down
   analysis before continuing the USE sweep over more resources.

5. At the end you should have evidence for whether the corrective action is to
   **adjust the load applied** or **tune the resource itself**.

## USE vs the Tools Method (why not just run tools?)

The "Tools Method": list available performance tools → list the useful metrics
each provides → list interpretation rules per metric. It produces a prescriptive
checklist, but it relies exclusively on *available or known* tools, which can
give an incomplete view of the system — and you remain unaware that your view
is incomplete. Iterating many tools also distracts from the goal.

USE inverts this: iterate over system *resources* to build a complete list of
questions, then find tools to answer them. Unknown areas are documented as
known-unknowns, giving you a to-do list for the day thorough analysis is really
needed.

## Beyond servers: the Apollo example

USE works on any system you have no expertise in. Gregg applied it to the
Apollo Lunar Module guidance system using only its functional block diagram:

- **AGC utilization**: CPU cycles doing real jobs (not the "DUMMY JOB") ÷ clock
  rate (2.048 MHz).

- **AGC saturation**: jobs occupying the seven "core set" register sets; once
  exhausted this escalates from saturation to error — the 1202 "EXECUTIVE
  OVERFLOW-NO CORE SETS" alarm (famously seen during the Apollo 11 descent,
  along with 1201 "NO VAC AREAS").

- **AGC errors**: the alarm set, e.g. 1203 "WAITLIST OVERFLOW-TOO MANY TASKS".

From "no idea where to start" to specific metrics in minutes — and the follow-on
analysis (workload characterization) found the rendezvous radar interrupting the
AGC with unnecessary work during descent.

## Scope and limits

USE finds **bottlenecks and errors** — certain problem types only. Use it early,
use it fast, and hand off to complementary methods (TSA, workload
characterization, drill-down, latency analysis, off-CPU analysis) for the rest.
