# TSA on Linux (reconstructed instructions)

> **Provenance note.** Brendan Gregg's OS-specific TSA checklist pages
> (`TSAmethod/tsa-linux.html`, `tsa-solaris.html`) were never published — they
> have been "Under construction" stubs since 2014. This document is a
> **reconstruction** of how to perform the six-state TSA Method on Linux,
> assembled from his TSA Method page, the USE-Linux checklist, and his Off-CPU
> Analysis work. The generic method lives in
> [tsa-method.md](tsa-method.md); this file is the Linux how-to.

Linux has **no microstate accounting**, so per-state time is assembled from
`/proc`, `perf`, and eBPF rather than read from one command.

## Step 0 — cheap on-CPU vs off-CPU split

`/usr/bin/time` (or bash `time`): `real − (user + sys)` = off-CPU (blocked)
time. Example: `tar` took 50.8s real but 1.0s user + 11.6s sys → ~38s blocked
(storage I/O). This single measurement often decides which half of the state
table matters.

## State-by-state measurement and investigation

### Executing (on-CPU)

- **Measure:** `pidstat 1` / `top` "%CPU", split `%usr` vs `%system`.
- **Investigate user time:** CPU profiling — `perf record -F 99 -a -g` → flame
  graph (see [offcpu-and-flame-graphs.md](offcpu-and-flame-graphs.md)).

- **Investigate system time:** syscall rates (`syscount`, `strace -c` off
  production), kernel CPU profiles.

- Remember: CPU time can include **spinning on a lock** — spinning appears as
  Executing, not Lock.

### Runnable (run-queue latency)

- **Measure per-process:** `/proc/PID/schedstat` 2nd field
  (`sched_info.run_delay`, ns); `perf sched latency` (average/max delay per
  schedule); `perf sched timehist` (separate "Sch Delay" column next to "wait
  time"). bcc: `runqlat`, `runqlen`.

- **Confirm cause system-wide:** `mpstat -P ALL 1`, `vmstat 1` "r" vs CPU
  count; check **CPU resource controls** (cgroup `cpu.max`/quotas — the
  classic cloud throttle) and processor binding (`taskset`, cpusets).

- Caveat: Linux `vmstat` "r" counts running **and** waiting threads; load
  averages include uninterruptible I/O tasks — don't use them for CPU
  saturation.

### Anonymous Paging (Linux "swapping")

- **Measure per-process:** minor-fault rate — 10th field (`min_flt`) of
  `/proc/PID/stat` (also `htop` MINFLT).

- **Confirm system-wide:** `vmstat 1` "si"/"so"; `sar -B`
  "pgscank"+"pgscand"; `sar -W`; OOM events via `dmesg | grep killed`.

- **Investigate:** system-wide free memory, memory resource controls (cgroup
  `memory.max`), application growth/leaks. Usually a quick win: app grew
  beyond main memory → fix config, restart.

### Sleeping (blocked on I/O)

This is **Off-CPU Analysis** territory (full detail:
[offcpu-and-flame-graphs.md](offcpu-and-flame-graphs.md)).

- **Measure:** `cpudist -O -p PID` (bcc, ≥4.4) — off-CPU duration histogram;
  `offcputime -p PID` (bcc, ≥4.8) — off-CPU time by user+kernel stack
  (`-K` kernel-only, `-d` delimiter, `-f` folded for
  `flamegraph.pl --color=io`); `perf sched timehist` "wait time" column
  (higher overhead — event dump vs eBPF in-kernel summaries).

- **Filter noise:** involuntary context switches pollute stacks on
  CPU-saturated systems. They happen in `TASK_RUNNING (0)`; the interesting
  blocking states are `TASK_INTERRUPTIBLE (1)` and `TASK_UNINTERRUPTIBLE (2)`
  → `offcputime --state 2`.

- **Follow the stack to a resource:** a stack ending in `io_schedule()` →
  storage → `iostat -xnz 1`; network → `ss`, `tcplife`; page-ins → memory.

- **Pre-eBPF fallbacks:** SystemTap, perf event logging, sampling
  `/proc/PID/stack`.

### Lock

- **User-level:** `valgrind --tool=drd` (held time, contention, errors);
  dynamic tracing of `pthread_mutex_lock()`; off-CPU stacks ending in
  `futex`/`pthread_cond_timedwait`.

- **Kernel-level:** `/proc/lock_stat` waittime (needs `CONFIG_LOCK_STATS=y`);
  bcc `deadlock_detector`.

- **Spinning locks** appear as CPU time → find via CPU profiling
  (`perf record -a -g -F 997`) and off-CPU stacks.

### Idle (waiting for work)

No direct kernel state — the kernel sees Idle as Sleeping or Lock. Identify by
reading off-CPU stacks and separating "waiting for new work" stacks (socket
accept/epoll wait, background timers) from request-synchronous blocking.
Optionally instrument application request functions (e.g. MySQL's
`do_command()`) and record off-CPU time only during requests. Usually skip
Idle and move to the next most common state.

## Linux limitations vs microstate-accounting OSes

- No per-state high-resolution counters — everything above is assembly work.
- Off-CPU time **includes** scheduler (run-queue) latency after wakeup; tease
  apart with extra trace events if needed (CPU saturation is usually obvious
  anyway).

- Overhead: scheduler events can hit millions/sec. Benchmarks: perf event-dump
  tracing cost 9–13% throughput for tens of seconds plus large captures; eBPF
  in-kernel counting ~6% and scales sub-linearly with duration. Start with
  0.1s traces; watch `vmstat` "cs".

- Stack-trace prerequisites: `-fomit-frame-pointer` breaks user stack walking
  (`[unknown]` frames); JIT runtimes need `-XX:+PreserveFramePointer` / perf
  maps.

- Wakeup analysis: `wakeuptime` and `offwaketime` pair waker stacks with
  blocked stacks when the off-CPU stack doesn't reveal the reason.

## Appendix: TSA on Solaris — the easy case (for contrast)

Solaris microstate accounting divides thread time into 10 states with
high-resolution counters — most of the TSA work for free, in one command:

```text
prstat -mLc 1        # -m microstates, -L per-thread, -c don't clear; -p PID to target
```

Columns → TSA mapping (from Gregg's summary figure):

| prstat fields | TSA state | Analyze with | Typical actions |
| --- | --- | --- | --- |
| USR+SYS | Executing | DTrace stack profiles, flame graphs; `cpustat` stall cycles; syscall analysis for SYS | Fix inefficiencies; tunables/config in active code |
| DFL | Anonymous Paging | `vmstat -p 1` "api"; `vmstat 1` free; `zonememstat` resource controls | Add memory; raise limits; fix leaks/growth |
| LCK | Lock + Idle | CPU stack profiles for spins; DTrace [p]lockstat; `sched:::off-cpu` + `ustack()` to split Idle | Check config |
| SLP | Sleep + Idle | `iostat -xnz 1`, `nicstat 1`; DTrace `sched:::off-cpu` with `ustack()`/`stack()` | Tune or upgrade the resource |
| LAT | Runnable | `mpstat 1`; resource controls (`prctl`, `kstat -p caps::cpucaps_zone*`); pbind/psets | Add CPUs; raise limits; move/tune other load; unbind |

Worked example from the figure: USR 35 + SYS 28, DFL 22, SLP 16 → 63%
Executing, 22% Anonymous Paging, 16% Sleep. Investigate in that order — but
Anonymous Paging is usually the quick fix (app grew past main memory: config
edit + restart), so take it first and re-measure.
