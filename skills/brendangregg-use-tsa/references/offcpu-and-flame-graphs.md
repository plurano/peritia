# Off-CPU Analysis & Flame Graphs

> Distilled from Brendan Gregg, "Off-CPU Analysis"
> (<https://www.brendangregg.com/offcpuanalysis.html>) and "Flame Graphs"
> (<https://www.brendangregg.com/flamegraphs.html> plus the CPU flame graphs
> page and the differential flame graphs post). These are the follow-on
> techniques once USE/TSA have pointed at an area.

## Off-CPU Analysis

### What and why

All thread time divides into **on-CPU** (executing) and**off-CPU** (blocked or
waiting: I/O, locks, timers, paging, scheduler latency). CPU sampling only
observes threads while running — it gathers *nothing* while a thread is
blocked. Off-CPU analysis measures **off-CPU time together with stack traces**
to identify *why* threads blocked, so the two techniques together account for
100% of thread time.

Any off-CPU event on a synchronous request path is directly injected request
latency. Because it instruments the **kernel scheduler's** notion of blocking
(context-switch functions) rather than application functions, it works for any
blocking event in any application without knowing the application.

### Categories of off-CPU time

- Block/storage I/O (e.g. `io_schedule()` in a read path), network I/O
- Lock contention
- Timers/sleeps (`nanosleep`), waiting for work (thread pools polling)
- Paging/swapping
- **Scheduler (run-queue) latency**: after wakeup, a thread may wait for a CPU
  turn when CPUs are saturated; this time is *included* in off-CPU time.

- **Involuntary context switches**: the kernel preempts a thread at its
  timeslice (common under CPU saturation); the captured stack shows no logical
  reason to block — a tell-tale sign. On Linux these occur in state
  `TASK_RUNNING (0)`; interesting blocking events are `TASK_INTERRUPTIBLE (1)`
  and `TASK_UNINTERRUPTIBLE (2)` (filter: `offcputime --state 2`).

- Interrupts

### Procedure (Linux/eBPF)

1. **Fix stack traces first.** `-fomit-frame-pointer` breaks frame-pointer
   stack walking (symptom: `[unknown]` user frames); JIT runtimes need symbol
   help (`-XX:+PreserveFramePointer`, perf maps).

2. **Measure total off-CPU time as a sanity check.** Even `time(1)` shows it:
   `real − user − sys` (e.g. tar: 50.8s elapsed, only 12.7s CPU → ~38s
   blocked). `cpudist -O` (bcc) gives an off-CPU duration histogram per
   process.

3. **Capture blocking stacks with context:** `offcputime -p <pid>` (bcc, Linux
   ≥ 4.8; `-K` kernel-only, `-d` delimiter between user/kernel stacks). Read
   stacks bottom-up: the syscall path and kernel function (`io_schedule`,
   `xfs_buf_submit_wait`, `schedule_timeout`+`__down`) reveal the block reason.

4. **Filter to request-synchronous context.** Idle-server output is flooded by
   background/wait-for-work stacks (e.g. idle mysqld's `srv_master_thread`).
   Post-filter the folded stacks (e.g. `grep do_command`) or instrument
   application request boundaries so only off-CPU time *during requests* is
   recorded.

5. **Visualize as an off-CPU flame graph** (see below).

### Tools by OS

- **Linux**: eBPF/bcc — `offcputime`, `cpudist`, `wakeuptime`, `offwaketime`.
  Preferred: does **in-kernel summarization**; needs ≥ 4.8 for stack support
  (`cpudist` ≥ 4.4). `perf` works (`perf sched timehist` shows per-event wait
  time) at higher overhead. SystemTap also viable. Kernel instrument point:
  `finish_task_switch()` via kprobes or sched tracepoints.

- **Solaris**: DTrace `sched:::off-cpu` provider (origin of the technique,
  ~2005).

- **FreeBSD**: DTrace, plus `procstat -ka` kernel-stack sampling.

### Caveats (read before running in production)

- **Overhead is the #1 caveat.** Scheduler events can hit millions/sec;
  per-event tracing that dumps to user space generates GBs/min. Benchmark
  (8-CPU, 102k context-switches/s, CPU-saturated MySQL, 10s trace): **perf** =
  9% throughput loss during trace + 13% for 35s post-processing (224 MB
  capture, growing linearly with duration); **eBPF** = 6% loss, ~6s
  post-process (only unique stacks are emitted). In-kernel aggregation is what
  makes the method production-practical.

- Safe procedure on unknown systems: trace **0.1s first**, ratchet up, and
  watch system CPU, application request rate and latency, and context-switch
  rate (`vmstat` "cs" column; typical production 20–50k/s). Beware feedback
  loops (tracer tracing itself) and very deep stacks.

- **Wakeup stacks**: the off-CPU stack shows the blocking path but often not
  the *reason* — that lives in the thread that issued the wakeup. Use
  `wakeuptime` (who wakes whom) and `offwaketime` (waker + blocked stack
  paired); chain graphs walk wakeup chains.

- Scheduler latency is folded into off-CPU time (tease apart with extra trace
  events if needed; CPU saturation is usually obvious anyway).

## Flame Graphs

### What it is

A visualization of hierarchical data, invented (Dec 2011) to render **sampled
stack traces** so the most frequent code paths pop out instantly. It solved the
"walls of text" problem: a real MySQL profile was 591,622 lines / 27,053
stacks, and the text summary misidentified the hot path; the flame graph showed
the truth (`JOIN::exec`) immediately. Reference implementation:
`github.com/brendangregg/FlameGraph` (Perl → interactive SVG); also built into
perf, Windows WPA/PerfView, and most commercial profilers.

### How to read one

- **Each rectangle = one stack frame** (a function).
- **Y-axis = stack depth**, from zero at the bottom. The **top edge is what was
  actually on-CPU**; everything beneath a frame is its ancestry.

- **X-axis = the sample population, sorted alphabetically — NOT the passage of
  time.** Left-to-right order is meaningless; the sort maximizes frame merging.

- **Width = total time/frequency** the frame appeared in the profile (its own
  samples plus descendants'). Wide frames may run long per call *or* just be
  called often — sample count and call count are not shown.

- **Colors are not significant by default** (random warm palettes). Variations
  assign meaning: hue by code type (`--colors=java`), green for memory graphs,
  io palette for off-CPU, red/blue for differentials.

- **Interactive SVG**: hover for details, **click to zoom**, **search**
  highlights matches and reports the cumulative % of stacks containing a term.

- Reading tips: scan for **wide plateaus** (a function burning samples
  directly, not via children); keep the folded intermediate file — it is
  greppable, so filter before rendering (`grep -v cpu_idle`, `grep ext4`).

### Types

| Type | Widths represent | Use |
| --- | --- | --- |
| **CPU** | On-CPU sample counts | Hot code paths (the original) |
| **Off-CPU** | Blocked time | Why threads wait; pairs with CPU graphs for 100% of time |
| **Memory** | Bytes (stacks at allocation) | Leak/growth analysis; conventionally green |
| **Hot/Cold** | On-CPU + off-CPU combined | One-graph overview |
| **Differential (red/blue)** | Second profile's widths; color = delta (red = growth, blue = reduction) | Regression analysis between two profiles |
| **CPI flame graphs** | Cycles vs stall-cycle deltas | Highlight memory stalls |

Related but different: **flame charts** (x-axis = time; some tools mislabel
these as flame graphs), **icicle charts** (inverted), **sunburst** (radial),
**FlameScope** (subsecond heat map → select ranges → flame graphs, for
perturbation study).

Differential workflow: `difffolded.pl folded1 folded2 | flamegraph.pl >
diff.svg`. Options: `-n` normalize counts (else load differences paint
everything), `-x` strip hex addresses, `--negate` swaps the scale (needed to
see paths that vanished in profile 2).

### Generation workflow (capture → fold → render)

Classic Linux perf:

```bash
git clone https://github.com/brendangregg/FlameGraph && cd FlameGraph
perf record -F 99 -a -g -- sleep 60          # 99 Hz, all CPUs, call graphs
perf script | ./stackcollapse-perf.pl > out.perf-folded
./flamegraph.pl out.perf-folded > perf.svg
```

- Use odd sample rates (99/199 Hz) to avoid lockstep sampling with timed
  activity.

- Lower-overhead eBPF route (Linux ≥ 4.8):
  `profile-bpfcc -F 99 -adf 60 > out.folded` (in-kernel aggregation; `-a`
  annotates kernel frames `_[k]`, `-d` user/kernel delimiter, `-f` folded),
  then `flamegraph.pl --colors=java`.

- DTrace (Solaris/BSD):
  `profile-99 /execname=="mysqld" && arg1/ { @[ustack()]=count(); }` →
  `stackcollapse.pl` → `flamegraph.pl`.

- Any profiler that emits stacks works (JFR, async-profiler, Instruments,
  Xperf): profiler-specific `stackcollapse-*.pl` converters exist.

## How the techniques compose

**USE** for a fast system-wide bottleneck sweep → **TSA** to decompose the
affected threads' time → **off-CPU analysis** when the time is blocked rather
than running → **flame graphs** as the visualization layer for on-CPU and
off-CPU stacks (plus memory and differential variants for allocation and
regression questions).
