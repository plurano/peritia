# Linux Observability Tools Map

> Transcribed and condensed from Brendan Gregg's Linux performance tool
> diagrams (<https://www.brendangregg.com/linuxperf.html>). Organized by the
> system layer each tool observes, so that once USE/TSA identify a suspect
> layer you know exactly which instruments exist for it. Prefer eBPF/bcc tools
> for production tracing (in-kernel aggregation = low overhead); prefer
> odd-numbered sample rates (99/199 Hz) to avoid lockstep with timed activity.

## General-purpose tracers (all layers)

| Tool | Purpose |
| --- | --- |
| `perf` | perf_events: profiling, tracing, CPU performance counters (PMCs) |
| `Ftrace` | In-kernel tracer (basis of Gregg's perf-tools) |
| `BCC` | eBPF compiler collection — the tracing toolkit below |
| `bpftrace` | High-level eBPF tracing language |
| `LTTng` | Linux trace toolkit |
| `sar` | System activity reporter — historical + live counters for every layer (`-u` CPU, `-P ALL` per-CPU, `-q` run-queue, `-r` memory, `-B` paging, `-S` swap, `-b`/`-d` block I/O, `-n DEV/TCP/ETCP/SOCK/...` network) |
| `/proc`, `dmesg`, `dstat` | System-wide state |

## Applications / libraries / syscall interface

| Tool | Purpose |
| --- | --- |
| `strace` | Syscall tracer (beware production overhead) |
| `ltrace` | Library-call tracer |
| `opensnoop` | Trace `open()` syscalls per process (eBPF) |
| `lsof` | List open files |
| `fatrace` | File access events (fanotify) |
| `filelife` | Short-lived file lifespans |
| `pcstat` | Page-cache residency of files |
| `gethostlatency` | DNS resolution latency |
| `filetop`, `fileslower`, `vfscount`, `vfsstat`, `cachestat`, `cachetop`, `dcstat`, `dcsnoop`, `statsnoop`, `syncsnoop`, `mountsnoop` | bcc file/VFS/cache tools |
| `memleak` | bcc memory-leak tracing |
| Language runtimes | `ucalls`, `uflow`, `ugc`, `uobjnew`, `ustat`, `uthreads` (java/node/php/python/ruby) |

## File systems / VFS

| Tool | Purpose |
| --- | --- |
| `ext4dist`, `ext4slower` | ext4 latency histograms / slow operations (variants: `btrfs*`, `xfs*`, `zfs*`, `nfs*`) |
| `mdflush` | md flush events |

## Block devices / disks

| Tool | Purpose |
| --- | --- |
| `iostat` | Disk I/O stats (`-xz 1`: `await`, `avgqu-sz`, `%util`) |
| `biosnoop` | Per-I/O trace with latency |
| `biolatency` | Block I/O latency histogram |
| `biotop` | top(1) for block I/O |
| `bitesize` | I/O size histogram |
| `blktrace` | Detailed block-layer event trace |

## Scheduler / CPUs

| Tool | Purpose |
| --- | --- |
| `mpstat` | Per-CPU stats (`-P ALL`) |
| `runqlen`, `runqlat` | Run-queue length / latency (saturation) |
| `cpudist` | On-/off-CPU time histograms |
| `offcputime` | Off-CPU time by stack trace |
| `wakeuptime`, `offwaketime` | Wakeup stacks; waker+blocked pairs |
| `profile` | Timed kernel stack sampling (CPU flame graphs) |
| `execsnoop` | New process execution |
| `softirqs`, `hardirqs` | Soft/hard IRQ time |
| `top`, `atop`, `ps`, `pidstat` | Process CPU/memory usage |
| `turbostat`, `showboost`, `rdmsr` | CPU frequency/turbo/MSRs (hardware level) |
| `llcstat` | Last-level cache hit ratio by CPU |
| `deadlock_detector`, `cpuunclaimed` | bcc scheduler extras |

## Memory (VM / DRAM)

| Tool | Purpose |
| --- | --- |
| `vmstat` | Virtual memory + CPU summaries (`si`/`so`, `free`, `wa`) |
| `free` | Memory usage (`-m`; buffers/cache) |
| `slabtop`, `slabratetop` | Kernel slab cache usage |
| `numastat` | NUMA memory stats |
| `oomkill` | Trace OOM-killer events |
| `swapon` | Swap device listing |
| `tiptop`, `perf` | Per-CPU PMCs (IPC, stall cycles) |

## Interrupts

`hardirqs`, `softirqs`, `criticalstat` (IRQ-off latency / critical sections).

## Network

| Tool | Purpose |
| --- | --- |
| `ss` | Socket statistics |
| `nstat`, `netstat` | Network counters |
| `nicstat`, `ip -s link`, `sar -n DEV` | Interface throughput/utilization |
| `ethtool` | NIC/driver stats |
| `tcpdump` | Packet capture |
| `tcptop`, `tcplife`, `tcptracer`, `tcpconnect`, `tcpaccept`, `tcpconnlat`, `tcpretrans`, `tcpdrop`, `udpconnect` | bcc TCP/UDP tools (sessions, latency, retransmits) |
| `snmpget`, `lldptool` | SNMP counters; LLDP/DCBX link info |

## Static performance analysis (no load applied)

Check configuration and inventory before/without running anything: `df`,
`ldd`, `mdadm`, `dmesg`, `schedtool`, `tc`, `ip`, `route`, `swapon`, `lspci`,
`lsblk`, `lsscsi`, `blockdev`, `smartctl`, `fdisk -l`, `lsusb`, `lldptool`,
`ethtool`, `/proc/cpuinfo`, `lscpu`, `numactl`, `lstopo`, `dmidecode`,
`lshw`, `sysctl` + `/sys`.

## Benchmarking tools (for controlled experiments)

HTTP/app: `ab`, `wrk`, `jmeter` · System: `sysbench`, `UnixBench`, `lmbench`,
`perf bench`, `pts` · Crypto: `openssl speed` · Network: `iperf`, `ttcp`,
`hping3`, `ping`, `traceroute`, `mtr` · Storage: `fio`, `dd`, `hdparm` ·
ML: `MLPerf`. Always apply **Active Benchmarking**: analyze the system while
the benchmark runs and determine its limiting factors.

## Tuning tools (once root cause is known)

App config/env · `nice`, `renice`, `taskset`, cpuset, `ulimit` · `sysctl` +
`/sys` · `numactl` · `ionice` · `ip`, `route`, `tc` · `ethtool` · `tune2fs` ·
`swapon` · `hdparm`, `blockdev` · `chcpu`.

## Gotchas worth remembering

- **Linux load averages include uninterruptible (disk) sleep** — they are not
  a pure CPU metric; use `vmstat`'s `r` for CPU saturation.

- **%CPU can mislead**: memory stall cycles count as busy CPU; check IPC/PMC
  data before concluding "CPU-bound".

- **strace overhead** can be severe in production; prefer eBPF equivalents.
- **Frame pointers** matter: `-fomit-frame-pointer` builds break stack
  walking; JIT runtimes need `-XX:+PreserveFramePointer` / perf maps.

- Diagram years differ (observability 2021, bcc 2018, bpftrace 2019); newer
  bcc tools exist beyond this map. Check
  <https://github.com/iovisor/bcc> for the current set.
