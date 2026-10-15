# The Methodology Toolbox

> Distilled from Brendan Gregg, "Performance Analysis Methodology"
> (<https://www.brendangregg.com/methodology.html>). USE and TSA are two tools;
> this is the rest of the box. A methodology is "a procedure that you can
> follow to analyze system or application performance... a starting point and
> then guidance to root cause." Analysis without one becomes a fishing
> expedition.

## Gregg's recommended try-order

1. **Problem Statement Method** — define the problem
2. **Workload Characterization Method** — understand the applied load
3. **USE Method** — find the bottlenecked resource
4. **CPU Profile Method** — understand on-CPU time
5. **Off-CPU Analysis** — understand blocked time
6. **Time Division Method** — decompose latency

Methodologies also tell you **when to give up**: try several on a given issue
or theory before moving on.

## Anti-methodologies (recognize and avoid)

| Anti-method | Pattern | Why it fails |
| --- | --- | --- |
| **Blame-Someone-Else** | Hypothesize the issue is a component another team owns; redirect; repeat when proven wrong | Optimizes for deflecting blame; analysis is redirected, not performed |
| **Streetlight** | Pick familiar/random tools, run them, look for obvious issues | Searches where the light is, not where the problem is |
| **Drunk Man** | Change things at random until the problem goes away | No understanding; the problem (or a worse one) returns |
| **Random Change** | Baseline → change a random tunable one way → measure → the other way → measure → keep if better, repeat | Iterates blindly without identifying the bottleneck |
| **Passive Benchmarking** | Run a benchmark with various options, make slides, hand to management | No analysis of what limits the benchmark; contrast with Active Benchmarking |
| **Traffic Light** | Dashboard all green? All good. Something red? That's the problem | Treats thresholds as ground truth; no root-cause analysis |

## The methodologies

### Framing & definition

- **Ad Hoc Checklist Method** — "Run A, if B, do C." Fast for *known* problem
  classes (support-staff checklists); fails on novel issues.

- **Problem Statement Method** *(use first)* — turn a vague complaint into a
  defined problem:

  - What makes you think there is a performance problem?
  - Has this system ever performed well?
  - What has changed recently? (Software? Hardware? Load?)
  - Can the degradation be expressed in terms of latency or run time?
  - Does it affect other people or applications (or is it just you)?
  - What is the environment? Software, hardware, versions, configuration?
- **RTFM Method** — research priority order: man pages → books → web search →
  co-workers → talk slides/video → support services → source code →
  experimentation → social media.

- **Scientific Method** — question → hypothesis → prediction → test →
  analysis. Fallback when no direct methodology fits; form falsifiable
  theories.

- **OODA Loop** — observe → orient → decide → act (Boyd; popularized in tech
  by Rapoport/Netflix). Iterative loop for incident time pressure.

### Load & drill-down

- **Workload Characterization Method** — characterize the *applied* load (the
  problem may simply be excessive load): **Who** is causing it (PID, UID, IP,
  customer ID, region)? **Why** is it called (code path, stack trace, flame
  graph)? **What** is it (IOPS, throughput, type, URL)? **How** is it changing
  over time (time series)? *The best tuning targets are often found by
  eliminating unnecessary work, not speeding up necessary work.*

- **Drill-Down Analysis Method** — start at the highest level; examine
  next-level details; pick the most interesting breakdown; repeat until root
  cause. Peels layers of software/hardware following the biggest contributor
  (app latency → syscall → kernel → device).

- **Process of Elimination** — divide the target into components; repeatedly
  choose a quick test that can exonerate many untested components (ideally
  half). If the problem is isolated to one tested component, that component
  becomes the new target — recurse. *Note exonerated resources to narrow the
  search space* (pairs with USE).

- **By-Layer Method** — measure latency (as a histogram) at each layer:
  dynamic languages → executable → libraries → syscalls → kernel (FS, network)
  → device drivers. *Investigate the lowest layer where latency is introduced.*

### Latency & causality

- **Time Division Method** (formerly Latency Analysis) — (1) measure operation
  time; (2) divide into logical synchronous components; (3) continue dividing
  until the latency origin is identified; (4) quantify: estimate the speedup
  if fixed. *The core method for latency problems.*

- **5 Whys Performance Method** — given delivered performance, ask "why?",
  then ask "why?" of each answer, up to five deep. Symptom → root cause.

- **Method R** (Millsap & Holt, 2003) — select user actions that matter to the
  business; measure the causes of their response time; calculate the best
  net-payoff optimization (if insufficient gain, suspend tuning until something
  changes); repeat. *Response-time economics.*

### Resource & thread oriented (this skill's core)

- **USE Method** — for every resource: utilization, saturation, errors. See
  [use-method.md](use-method.md).

- **RED Method** (Tom Wilkie) — for every *service*: **R**equest rate,
  **E**rrors, **D**uration. The service-level analog of USE for
  microservices/request-driven systems.

- **TSA Method** — for every thread: time in Executing / Runnable / Swapping /
  Sleeping / Lock / Idle; investigate most-frequent first. See
  [tsa-method.md](tsa-method.md).

- **CPU Profile Method** — generate a CPU profile (ideally a flame graph);
  understand every software component above 1%.

- **Off-CPU Analysis** — profile per-thread off-CPU time with stack traces;
  coalesce like stacks; study largest to smallest. See
  [offcpu-and-flame-graphs.md](offcpu-and-flame-graphs.md).

- **Stack Profile Method** — profile all thread stacks, on- and off-CPU;
  coalesce; study bottom-up. Generalizes CPU/off-CPU profiling.

- **Tools Method** — list available tools → list useful metrics per tool →
  list interpretations per metric → run and interpret. A deliberate version of
  streetlight; still tool-driven, so it can miss what your tools cannot see.
  Useful when unfamiliar with a system.

### Benchmarking & planning

- **Active Benchmarking Method** — configure the benchmark to run for a long
  duration; *while it runs*, analyze the system with other tools and determine
  the limiting factors. Benchmark the benchmark.

- **Benchmarking Checklist** (Gregg) — before trusting a result: Why not
  double? (did it scale as expected) · Did it break limits? · Did it error? ·
  Does it reproduce? · Does it matter? · Did it even happen?

- **Performance Evaluation Steps** (Jain 1991) — state goals & boundaries →
  list services/outcomes → select metrics → list parameters → select factors →
  select workload → design experiments → analyze/interpret → present → repeat
  if needed.

- **Capacity Planning Process** (Jain 1991) — instrument → monitor →
  characterize workload → predict under alternatives → select the
  lowest-cost/highest-performance option.

- **Intel Hierarchical Top-Down** (Ahmad Yasin) — CPU pipeline triage with
  PMCs: UOPs issued? retired? Classify CPU-bound time into retiring / bad
  speculation / front-end bound / back-end bound.

### Performance Mantras (Hanson & Crain)

When it comes to fixing, in rough order of preference:

1. **Don't do it** — eliminate the work
2. **Do it, but don't do it again** — caching/memoization
3. **Do it less**
4. **Do it later**
5. **Do it when they're not looking** — off-peak/background
6. **Do it concurrently**
7. **Do it cheaper**

## Choosing among them

| Situation | Start with |
| --- | --- |
| Vague complaint, no data yet | Problem Statement Method |
| "System is slow", suspect the box | USE Method (+ Process of Elimination) |
| One application/request is slow | TSA Method → state-specific tools |
| Threads burn CPU | CPU Profile Method (flame graph) |
| Threads wait/block | Off-CPU Analysis |
| Latency is the complaint | Time Division Method; By-Layer Method |
| Microservices estate | RED Method per service |
| "Is this benchmark result real?" | Benchmarking Checklist; Active Benchmarking |
| Nothing fits | Scientific Method; 5 Whys; OODA under pressure |
