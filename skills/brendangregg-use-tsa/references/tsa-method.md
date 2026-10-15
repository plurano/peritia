# The TSA Method (Thread State Analysis)

> Distilled from Brendan Gregg, "The TSA Method"
> (<https://www.brendangregg.com/tsamethod.html>). Complementary to the
> resource-oriented USE Method: TSA is **thread-oriented**. Read this when you
> need the full method; `SKILL.md` only carries the operating procedure.

## One-line summary

1. **For each thread of interest, measure total time in different thread
   states.**

2. **Investigate states from the most to the least frequent, using appropriate
   tools.**

"Thread" here means any OS runnable entity — thread, task, or process,
application or kernel. TSA is applicable to any OS because it is developed from
the questions we would like answered: *where is each thread spending its time?*

Where USE asks "how busy is each resource, and is work queued behind it?", TSA
asks "for the threads that matter, what kind of waiting (or working) dominates
their time?". Deploy both early in an investigation to get an initial handle on
performance.

## The six states

Thread time is divided into exclusive states. Six, based on a generic OS
kernel, identify the key sources of performance issues:

| State | Description | How to investigate it |
| --- | --- | --- |
| **Executing** | Running on-CPU | Split into user and system time. User time: CPU profilers to identify hot code paths (flame graphs). System time: examine syscall rates, profile kernel CPU time. Note CPU time can include spinning on a lock. |
| **Runnable** | Run queue latency — waiting for a turn on-CPU | Check system-wide CPU utilization and saturation, for physical CPUs *and* any imposed resource controls (USE Method). Check processor binding, which can affect scheduling. |
| **Anonymous Paging** | Runnable, but swapped-out or paged-out, waiting for residency | Check system-wide main memory availability and any memory resource controls. Study paging/swapping saturation metrics (USE Method). |
| **Sleeping** | Waiting for I/O — network, block, and data/text page-ins | Syscalls: identify time in syscalls and the related resource; check mmap() usage and non-syscall I/O via mappings. Resource usage: USE Method to find busy resources. Thread blocking: trace scheduler off-CPU events with user+kernel stacks (Off-CPU Analysis). |
| **Lock** | Waiting to acquire a synchronization lock (waiting on someone else) | Identify the lock the thread is waiting on and the reason acquisition took time (lock analysis). |
| **Idle** | Waiting for work | Check the client load applied. |

Terminology:

- **Anonymous paging**: paging = movement of small units of memory (e.g.
  4 Kbyte pages). "Anonymous" = working memory with no named file-system
  location. Linux calls this "swapping" (which means something else on other
  OSes).

- **Sleeping** is the Unix term for leaving CPU to wait for an event; the
  six-state model separates Lock and Idle out of it.

Kernels manage most of these states directly, so there are code paths that can
be instrumented — if they are not already — to provide state timing for any
thread.

## The Idle state caveat

Modern OSes offer no easy way to identify the Idle state. Application threads
wait for work in many ways — a new network connection, network I/O, a lock, a
timer — and from the kernel's perspective the thread looks Sleeping or Lock.
Only after some analysis does the time reveal itself as really Idle.

In many cases Idle can be skipped even though it is the most common state: an
application may process requests 10% of the time and be Idle 90%. For TSA,
investigate the most common states *after* Idle.

## Latency states — tune these first

The **Runnable, Anonymous Paging, Sleeping, and Lock** states measure time
spent *waiting while performing work* — latency. Tuning these often gives the
greatest wins because their state time can be reduced to zero, and they are
frequently the easiest wins given the right tools.

Rule of thumb: **if more than 10% of time is in Runnable or Anonymous Paging,
tune and eliminate those states first.** This simplifies analysis of the
remaining states — fewer factors at play. (Anonymous paging is often an
application misconfiguration: it has grown beyond main memory and the OS pages
it out. The fix is often a config edit and a restart.)

## Subdividing further (optional)

It can be tempting to add states to narrow the initial breakdown:

- **Executing** → split into User and System (kernel) execution.
- **Sleeping** → split into Storage, Network, and Other waits.
- Memory stall cycles could theoretically be split out of Executing, leaving it
  as non-stalled instruction cycles — difficult to implement.

Check what states your OS already provides and how to read them (e.g. macOS
Instruments exposes "Waiting" and "Waiting uninterruptibly"). In practice the
six-state approach is an effective first-level breakdown; subdivide naturally
as the investigation proceeds.

## Example usage

*A cloud-hosted application has a performance issue.* Thread state time is
measured for its threads across the six states: ~50% of time is **Runnable**.
State-appropriate tools follow: `mpstat(1)` shows the CPUs are not overloaded,
so CPU *resource controls* are checked — and the application is found to be
throttled by a CPU limit, normal in that cloud environment. The limit is
raised; performance is resolved. TSA provided the starting point and then the
direction.

## Where time is *really* spent (why TSA beats component timers)

Some monitoring software breaks down time by application component — "time
spent in MySQL", "time spent in PHP". That is **request-oriented**, measured
from when a component began servicing work to when it completed, and it can
mislead. Suppose "time spent in MySQL" is high: TSA on MySQL's threads may show
them largely **Runnable** because another tenant is stealing CPU cycles. That
time was not spent executing MySQL at all — reporting it as MySQL time sends
the investigation to the wrong place. TSA prevents misdirected investigations.

## Example: TSA on Solaris (microstate accounting)

Solaris has microstate accounting — thread time divided into 10 states with
high-resolution counters — which does most of the TSA work for you:

```text
prstat -mLc 1        # -m microstates, -L per-thread, -c don't clear, 1s interval
prstat -mLc -p PID 1 # target a single process
```

Reading a sample output: 63% Execution (35% user + 28% system), 22% Anonymous
Paging, 16% Sleep → investigate in that order (or fix the quick Anonymous
Paging win first, then re-measure). Execution time is understood with flame
graphs (user and system separately). The Sleep time may include Idle — use
DTrace off-CPU analysis with stack traces and off-CPU durations (also
renderable as flame graphs); splitting out Idle requires reading stacks to
distinguish blocking *during work* from *waiting for new work*.

## Historical note (why the states look familiar)

The six states are inspired by the Unix process state transition diagram
[Bach 86]. The notion is older: TENEX (c. 1969–72) had Control-T / RUNSTAT
printing job states ("RUNNING", "IO WAIT", "SLEEP", "FORK WAIT"); RSTS/E grew
a 16-state breakdown by 1985. What those systems lacked was cumulative state
*timing* — microstate accounting (Solaris 8, c. 2000) is the feature that makes
TSA direct rather than sampled.

These states assume a **time-shared** OS (Unix, Linux, Windows). On
non-time-shared systems (e.g. a UNIVAC running one program that waits on I/O
mid-instruction) the states must be redefined — Sleeping on I/O is not then
exclusive from Executing.

## Method relationships

- **TSA vs USE**: different perspectives (threads vs resources); both are
  starting points that narrow the investigation. Run both early.

- **TSA vs Method R / Oracle Wait Interface**: Method R breaks a *query* into
  states; TSA breaks a *thread* into OS states. Latency-based methods approach
  100% of issues but demand application-internals familiarity and more time.

- **Off-CPU Analysis** analyzes Sleeping, Lock, Runnable, and Anonymous Paging.
  Applications do not *choose* to enter Runnable and Anonymous Paging, so the
  user-level stack will not identify them — the kernel-level stack will.

- **Flame graphs** analyze the Executing state (and off-CPU states via their
  own graph type).

## References

- [Bobrow 72] Bobrow, D., et al, "TENEX, A Paged Time Sharing System for the
  PDP-10", Communications of the ACM, March 1972.

- RSTS/E System User's Guide, 1985.
- [Bach 86] Bach, M.J., "The Design of the UNIX Operating System", Prentice
  Hall, 1986.

- [Millsap 03] Millsap, C., Holt, J., "Optimizing Oracle Performance",
  O'Reilly, 2003.

- [Gregg 13] Gregg, B., "Systems Performance: Enterprise and the Cloud",
  Prentice Hall, 2013.
