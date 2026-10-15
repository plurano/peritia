# Linux: The First 60 Seconds (60,000 Milliseconds)

> Distilled from Brendan Gregg / Netflix, "Linux Performance Analysis in
> 60,000 Milliseconds" (2015). What to run in the first minute after landing on
> a Linux server with a performance issue. Some commands need the **sysstat**
> package.

Premise: **look for errors and saturation first** (both easy to interpret),
then resource utilization. Saturation = a resource has more load than it can
handle, exposed as queue length or time spent waiting. These ten commands
complete a good part of the USE Method; as you go, **note which resources you
have checked and exonerated** — process of elimination narrows the targets.

## The ten commands

```text
uptime
dmesg | tail
vmstat 1
mpstat -P ALL 1
pidstat 1
iostat -xz 1
free -m
sar -n DEV 1
sar -n TCP,ETCP 1
top
```

## What each one tells you

### 1. `uptime`

Load averages = number of tasks wanting to run; on Linux this **includes tasks
blocked in uninterruptible I/O** (usually disk). The three numbers are
exponentially damped moving sums over 1/5/15 minutes — they show the *trend*
(1-min ≪ 15-min means you logged in too late). High-level demand only; worth a
quick look only.

### 2. `dmesg | tail`

Last kernel messages. Look for errors that cause performance issues: the
**oom-killer** killing a process, "TCP: Possible SYN flooding... Dropping
request", hardware errors. **Don't miss this step** — dmesg is always worth
checking.

### 3. `vmstat 1`

One line per second (skip the first line — it is the since-boot average).

- **`r`** — processes running and waiting for a turn. A better CPU-saturation
  signal than load averages (excludes I/O). **`r` > CPU count = saturation.**

- **`free`** — free memory in KB.
- **`si`, `so`** — swap-ins/outs; non-zero = out of memory.
- **`us`, `sy`, `id`, `wa`, `st`** — user, system, idle, wait-I/O, stolen
  time. `us`+`sy` confirms CPUs are busy; constant `wa` points to a disk
  bottleneck; **`sy` averaging over ~20%** is worth exploring (kernel may be
  processing I/O inefficiently).

### 4. `mpstat -P ALL 1`

Per-CPU time breakdown (`%usr %sys %iowait %irq %soft %steal %idle`). Check
for **imbalance** — a single hot CPU is evidence of a single-threaded
application.

### 5. `pidstat 1`

Rolling per-process CPU summary — like `top` but it doesn't clear the screen,
so patterns over time survive (and copy-paste into the investigation record is
easy). `%CPU` is totaled across all CPUs (1591% ≈ 16 CPUs).

### 6. `iostat -xz 1`

Block-device workload and resulting performance.

- **`r/s`, `w/s`, `rkB/s`, `wkB/s`** — delivered workload (workload
  characterization: the problem may simply be excessive applied load).

- **`await`** — average I/O time in ms, queueing + service: **the time the
  application suffers**. Higher than expected = device saturation or device
  problems.

- **`avgqu-sz`** — average requests issued to the device; **> 1 can be
  evidence of saturation** (caveat: devices process requests in parallel,
  especially virtual devices fronting many back-end disks).

- **`%util`** — device busy percent; **> 60% typically leads to poor
  performance** (visible in `await`); ~100% usually indicates saturation (but
  on a logical device fronting many disks it may just mean I/O is always in
  flight).

Poor disk I/O isn't necessarily an application issue — read-ahead and write
buffering make I/O asynchronous.

### 7. `free -m`

- **`buffers`** = buffer cache (block device I/O); **`cached`** = page cache
  (file systems). Near-zero caches → higher disk I/O (confirm with `iostat`).

- The **`-/+ buffers/cache`** line gives the less-confusing used/free view —
  cache memory is reclaimable.

- ZFS-on-Linux caveat: ZFS's own ARC isn't reflected properly, so the system
  can look low on free memory when it isn't.

### 8. `sar -n DEV 1`

Network interface throughput: **`rxkB/s`, `txkB/s`** as workload measure and
to check whether a link limit is reached (e.g. 22 MB/s receive = 176 Mbit/s,
under a 1 Gbit/s limit). **`%ifutil`** = interface utilization (max of both
directions for full duplex).

### 9. `sar -n TCP,ETCP 1`

- **`active/s`** — locally initiated connections/s (`connect()`; "outbound").
- **`passive/s`** — remotely initiated connections/s (`accept()`; "inbound").
- **`retrans/s`** — TCP retransmits/s: a sign of a network or server issue —
  an unreliable network, or a server overloaded and dropping packets.

### 10. `top`

Includes most metrics already checked. Run it to see whether anything looks
**wildly different** from the earlier commands — that indicates variable load.
Downside: patterns over time are hard to see and intermittent evidence is lost
when the screen clears (Ctrl-S pauses, Ctrl-Q resumes).

## After the first minute

The checklist bounds what you *don't* know: every resource you checked is
exonerated (for now), everything unchecked is a documented known-unknown.
Next steps by finding:

- CPU saturated → [CPU profiling + flame graphs](offcpu-and-flame-graphs.md),
  [TSA for the hot threads](tsa-linux.md)

- Threads blocked → [Off-CPU analysis](offcpu-and-flame-graphs.md)
- Memory pressure → USE memory rows in
  [use-linux-checklist.md](use-linux-checklist.md)

- Network/disk errors → drill into the specific device with the tools in
  [linux-observability-tools.md](linux-observability-tools.md)
