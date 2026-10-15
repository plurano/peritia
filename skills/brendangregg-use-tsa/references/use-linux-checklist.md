# USE Method: Linux Checklist

> The complete Utilization–Saturation–Errors checklist for Linux, from Brendan
> Gregg's "USE Method: Linux Performance Checklist"
> (<https://www.brendangregg.com/USEmethod/use-linux.html>), with the Rosetta
> Stone cross-check (<https://www.brendangregg.com/USEmethod/use-rosetta.html>).
> For each system resource, metrics for utilization, saturation, and errors are
> identified and checked. Terms: **LPE** = Linux Performance Events (the `perf`
> command); **CPC** = CPU Performance Counters (aka PMCs/PICs/PMU events, read
> via `perf stat -e`).

## Physical resources

### CPU

| Type | Metric |
| --- | --- |
| utilization | System-wide: `vmstat 1` "us"+"sy"+"st"; `sar -u` (sum all except %idle, %iowait); `dstat -c` (except "idl", "wai"). Per-CPU: `mpstat -P ALL 1` (same fields); `sar -P ALL`. Per-process: `top`/`htop` "%CPU"; `ps -o pcpu`; `pidstat 1` "%CPU". Per-kernel-thread: `top`/`htop` ("K" to toggle), VIRT == 0 heuristic |
| saturation | System-wide: `vmstat 1` "r" > CPU count; `sar -q` "runq-sz" > CPU count; `dstat -p` "run". Per-process: `/proc/PID/schedstat` 2nd field (`sched_info.run_delay`); `perf sched latency` (average/max delay per schedule); dynamic tracing (e.g. SystemTap `schedtimes.stp` "queued(us)") |
| errors | `perf` (LPE) processor-specific error events (CPC) if available; e.g. AMD64 "Single-bit ECC Errors Recorded by Scrubber" |

Caveats: `%CPU` can be odd in virtualized environments. A single hot CPU can be
one hot thread or a mapped hardware interrupt — relief usually means
parallelizing. **`uptime` load average is deliberately excluded**: Linux load
averages include uninterruptible (usually disk I/O) tasks. `vmstat`'s "r"
counts running **and** waiting threads on Linux (the man page is misleading).

### Memory capacity

| Type | Metric |
| --- | --- |
| utilization | System-wide: `free -m` ("Mem:", "Swap:"); `vmstat 1` "free"/"swap"; `sar -r` "%memused"; `dstat -m`; `slabtop -s c` (kernel slab). Per-process: `top`/`htop` "RES" (resident) and "VIRT" (virtual) |
| saturation | System-wide: `vmstat 1` "si"/"so" (swapping); `sar -B` "pgscank"+"pgscand" (scanning); `sar -W`. Per-process: minor-fault rate, 10th field (`min_flt`) of `/proc/PID/stat` (also `htop` MINFLT), or dynamic tracing. OOM killer: `dmesg \| grep killed` |
| errors | `dmesg` for physical failures; dynamic tracing (e.g. SystemTap uprobes) for failed malloc()s |

### Network interfaces

| Type | Metric |
| --- | --- |
| utilization | `sar -n DEV 1` "rxKB/s"/max, "txKB/s"/max; `ip -s link` RX/TX throughput / max bandwidth; `/proc/net/dev` "bytes"; `nicstat` "%Util" |
| saturation | `ifconfig` "overruns", "dropped"; `netstat -s` "segments retransmited"; `sar -n EDEV` *drop and*fifo metrics; `/proc/net/dev` RX/TX "drop"; `nicstat` "Sat"; dynamic tracing for other TCP/IP stack queueing |
| errors | `ifconfig` "errors", "dropped"; `netstat -i` "RX-ERR"/"TX-ERR"; `ip -s link` "errors"; `sar -n EDEV` "rxerr/s" "txerr/s"; `/proc/net/dev` "errs", "drop"; extra counters under `/sys/class/net/...`; dynamic tracing of driver function returns |

Note: dropped packets appear as **both** saturation and errors.

### Storage device I/O

| Type | Metric |
| --- | --- |
| utilization | System-wide: `iostat -xz 1` "%util"; `sar -d` "%util". Per-process: `iotop`; `pidstat -d`; `/proc/PID/sched` "se.statistics.iowait_sum" |
| saturation | `iostat -xnz 1` "avgqu-sz" > 1, or high "await"; `sar -d` same; LPE block probes for queue length/latency; dynamic/static tracing of the I/O subsystem (block, SCSI, SATA layers; static LPE "scsi" and "block" tracepoints exist) |
| errors | `/sys/devices/.../ioerr_cnt`; `smartctl`; dynamic/static tracing of I/O subsystem response codes |

### Storage capacity

| Type | Metric |
| --- | --- |
| utilization | Swap: `swapon -s`; `free`; `/proc/meminfo` "SwapFree"/"SwapTotal". File systems: `df -h` |
| saturation | Doesn't really make sense — once full, ENOSPC |
| errors | `strace` for ENOSPC; dynamic tracing for ENOSPC; `/var/log/messages` errors, depending on FS |

### Controllers

| Resource | Type | Metric |
| --- | --- | --- |
| Storage controller | utilization | `iostat -xz 1`, sum devices and compare to known IOPS/throughput limits per card |
| Storage controller | saturation / errors | see storage device rows |
| Network controller | utilization | infer from `ip -s link` (or `/proc/net/dev`) vs known controller max throughput for its interfaces |
| Network controller | saturation / errors | see network interface rows |

### Interconnects (CPU / memory / I/O)

| Type | Metric |
| --- | --- |
| utilization | LPE (CPC) for interconnect ports/busses, throughput / max. Memory interconnect alternative: **CPI greater than ~5**; CPC may have local-vs-remote counters |
| saturation | LPE (CPC) for stall cycles |
| errors | LPE (CPC) for whatever is available |

I/O interconnect inference fallback: known throughput from `iostat`/`ip`.
Expect quality time with vendor manuals for the hex event codes.

## Software resources

### Kernel mutex

| Type | Metric |
| --- | --- |
| utilization | With `CONFIG_LOCK_STATS=y`: `/proc/lock_stat` "holdtime-total" / "acquisitions" (also "holdtime-min"/"max"); dynamic tracing of lock functions |
| saturation | `/proc/lock_stat` "waittime-total" / "contentions" (also "waittime-min"/"max"); spinning shows up in profiling (`perf record -a -g -F 997`) |
| errors | Dynamic tracing (e.g. recursive mutex enter); other errors cause kernel lockup/panic — debug with kdump/`crash` |

### User mutex

| Type | Metric |
| --- | --- |
| utilization | `valgrind --tool=drd --exclusive-threshold=...` (held time); dynamic tracing of lock-to-unlock time |
| saturation | `valgrind --tool=drd` to infer contention from held time; dynamic tracing of synchronization functions for wait time; profiling user stacks for spins |
| errors | `valgrind --tool=drd` error reports; dynamic tracing of `pthread_mutex_lock()` for EAGAIN, EINVAL, EPERM, EDEADLK, ENOMEM, EOWNERDEAD, ... |

### Task capacity

| Type | Metric |
| --- | --- |
| utilization | `top`/`htop` "Tasks" (current); `sysctl kernel.threads-max` (max) |
| saturation | Threads blocking on memory allocation; the page scanner should be running (`sar -B` "pgscan*"), else examine with dynamic tracing |
| errors | "can't fork()" errors; user threads: `pthread_create()` failures (EAGAIN, EINVAL, ...); kernel: dynamic tracing of `kernel_thread()` ENOMEM |

### File descriptors

| Type | Metric |
| --- | --- |
| utilization | System-wide: `sar -v` "file-nr" vs `/proc/sys/fs/file-max`; `dstat --fs`; or `/proc/sys/fs/file-nr`. Per-process: `ls /proc/PID/fd \| wc -l` vs `ulimit -n` |
| saturation | No real queueing — allocations error instead of blocking |
| errors | `strace` errno == EMFILE on fd-returning syscalls (open(), accept(), ...) |

## After the checklist

Follow up discoveries with **drill-down analysis** and**latency analysis**
(see [methodology-toolbox.md](methodology-toolbox.md)), and note every cell you
could not measure as a known-unknown.

## Other OSes (Rosetta Stone in one paragraph)

The Rosetta Stone covers Linux, Solaris, FreeBSD, and macOS with the same
resource × type matrix. The pattern holds everywhere; only the instruments
change: Solaris has `prstat -mLc`, `vmstat -p` ("api"/"apo" anonymous paging),
`nicstat`, `kstat`, `fmadm faulty`, `iostat -En`, DTrace everywhere and
`lockstat -H/-C/-E` + `plockstat` for locks; FreeBSD has `vmstat -P`,
`swapinfo`, `pstat -T`, `pmcstat`, DTrace; macOS has `vm_stat`,
`top -o cpu|mem`, Instruments (incl. the only GPU row in the matrix), DTrace.
Interconnects use hardware counters on all four (Linux `perf` CPC, Solaris
`cpustat`, FreeBSD `pmcstat`, macOS Instruments Counters). When on a non-Linux
host, rebuild this checklist by asking the same USE questions of whatever
instruments the OS provides — that act of reconstruction *is* the method.
