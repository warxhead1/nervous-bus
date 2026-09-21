# Orca session loss and memory pressure, September 20, 2026

Tracking: nervous-bus-gci8. Continued attribution coverage: nervous-bus-bnti.
All local times below are America/New_York (UTC-04:00).

## Findings

The recorded incident was not simple exhaustion of host RAM. Between
19:39:59 and 20:21:02, 42 minute samples show at least 18.39 GiB available
on the 62.01 GiB host. The shared Orca scope nevertheless held about 16 GiB
resident/cache memory and 8 GiB logical swap. Its policy set `MemoryHigh=16G`
and `MemorySwapMax=8G` for the entire main/daemon/agent/build process tree.
There were 3,245,710 high-threshold events in those sample intervals; the
cumulative PSI counter difference shows memory stalls during 40.73% of the
elapsed time. Host memory PSI was 43.54% over the same interval. These are
elapsed-time stall proportions, not the percentage of RAM consumed.

This is a measured reclaim problem with unused host headroom. Linux
[`memory.high`](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html)
throttles allocations and reclaims memory within the group; it does not
itself invoke an OOM kill. No OOM-kill record was found in the current kernel
ring, and surviving relevant cgroups reported zero OOM kills. That does not
rule out a userspace allocation failure or identify the sender of a signal.

## Recorded failure sequence

| Eastern time | UTC | Observation |
| --- | --- | --- |
| 20:21:10 | Sep 21 00:21:10 | New main process starts; its lifecycle breadcrumb is published later. |
| 20:21:23 | Sep 21 00:21:23 | systemd reports the old AppImage launcher's main process was killed with signal 9. The sender is absent from the retained record. |
| 20:21:23–28 | Sep 21 00:21:23–28 | Nine old Orca processes receive SIGBUS, including zygotes, GPU/network processes, session scanner and watcher workers. Their executable paths used the old AppImage mount. |
| 20:21:26 | Sep 21 00:21:26 | Old relocated terminal daemon still logs accepted client handshakes. This is evidence it survived the first event, not proof it could serve PTYs normally. |
| 20:21–22 | Sep 21 00:21–22 | New main logs repeated 30-second listSessions timeouts, then replacement after failed health checks with `liveSessions=unverifiable`. |
| 20:22:52 | Sep 21 00:22:52 | New terminal daemon starts; several prior terminal IDs spawn new session processes. Old-scope memory falls sharply at the following minute sample. |
| 20:23:00 | Sep 21 00:23:00 | Old Crashpad handler also receives SIGBUS. |

The source contains a replacement path that kills a positively identified
daemon after unsuccessful health/session probes. Its replacement is strongly
implicated in session loss; the exact initial kill origin remains unknown.
The closely timed AppImage-child SIGBUS failures are consistent with loss of
the mount backing their executables, but no fault-address/mapping analysis
has established that cause. They are not evidence of a kernel OOM kill.
The installed AppImage had not changed since September 19.

## Memory owners

The 20:49:54 snapshot reported **35.99 GiB used and 26.03 GiB available**.
These non-overlapping leaf cgroups include resident anonymous memory, charged
file cache, shared memory and kernel allocations. Logical swap is shown
separately and must not be added to physical RAM consumption.

| Workload | Resident/cache charge, GiB | Logical swap, GiB | Interpretation |
| --- | ---: | ---: | --- |
| Tachyon PostgreSQL | 9.45 | 0.07 | Approximately 8.15 GiB shared memory; configured shared_buffers is 8 GiB. Summing backend RSS would count shared pages repeatedly. |
| Current Orca main/daemon/agent/build group | 8.97 | 5.36 | Includes compilers, JVMs, test databases, Unreal jobs and agents; it is not GUI-only memory. |
| Hearth API | 2.16 | 1.27 | About 1.70 GiB of this cgroup charge was file cache. |
| WoW world server | 1.95 | 2.79 | Mostly private process memory. An earlier PSS census measured 1.94 GiB resident plus 2.77 GiB SwapPss. |
| Refactor neural | 0.74 | 2.54 | Embedding model loaded; generative models unloaded/not initialized in the sampled health response. |
| Beads Dolt | 0.54 | 0.94 | Separate database service. |

At that instant zram stored **34.57 GiB logical data in 10.53 GiB physical
memory**. That physical memory is already included in host usage. A complete
root-readable PSS census at about 20:32 accounted for shared resident pages
proportionally; it cannot reconstruct processes killed ten minutes earlier.
The old process tree's pre-incident 15.84 GiB of resident anonymous memory
therefore remains unattributed to individual processes.

GPU samples from 18:00 through 20:31 peaked at 5.38 GiB used on the 8 GiB
card. A 20:51:31 live read was 2,318 MiB. These observations do not implicate
VRAM exhaustion in this incident.

## Changes and validation

- At 20:40:23 the local game-priority watcher was backed up and its Orca
  `MemoryHigh` changed from 16 to 24 GiB. Existing 8 GiB swap and six-core CPU
  bounds remain. Python compilation and exact policy selection were checked;
  all four existing Orca scopes received the property. The main and daemon
  PID start times were identical before and after. This is interim headroom
  for the measured footprint, not proof of reduced application allocations
  or a completed solution to shared-group isolation.
- A 12-hour, at-most-256-event signal recorder was started. It records only
  selected signals for Orca-related process names, generator PID/name and
  target PID/name, with monotonic timestamps. No command lines, environments,
  terminal contents or cores are captured. A separately created probe
  process verified signal attribution. Normal AppImage CLI SIGTERM exits
  are excluded. The recorder was approximately 66 MiB resident after setup.
- An isolated Orca worktree backports upstream
  [terminal producer backpressure](https://github.com/stablyai/orca/commit/84d827a6abe0b3dc1d84ab2f434fe030cc69280f)
  and [bounded browser-debug output](https://github.com/stablyai/orca/commit/a445abadd4b2fd75d987872c0aa9d274f05ab33b).
  Source revision `852cbd8623` identifies version `1.4.205+memory.20260920.1`.
  The 126 tests in 12 focused files, node typecheck, changed-code quality and
  complete desktop build passed. Real Linux socket reproductions compared
  exact base `7a662843e5` with the backport under Node 26.8.2. The base failed
  the terminal producer-pause assertion; the fix passed and drained all
  queues after the reader resumed. The browser baseline retained 131,080,000
  socket bytes after 128 MiB input; the fix held about 8 MiB on the socket,
  then released the overflow backlog and disconnected that client. This
  proves retention mechanisms, not their contribution to this machine's
  unmeasured pre-crash allocation spike. Package/delivery evidence is recorded
  in the private receipt directory rather than inferred from source tests.

## Highest-value remaining work

1. Separate heavy PTY workloads from Orca control processes, and make daemon
   recovery preserve unverifiable sessions. The current replacement behavior
   can still turn a stall into session loss. Raising a shared threshold does
   not establish that invariant.
2. Profile Tachyon query work before changing its buffer pool. Active query
   samples identified universe seed selection, an equity latest/history
   query, and mathaudit's recursive asset count (over six minutes old).
   Database counters showed 1,988 GiB of cumulative temporary writes; their
   reset timestamp was null, so this is not a two-hour total. Settings were
   shared_buffers 8 GiB, work_mem 64 MiB per operation, maintenance_work_mem
   1 GiB, and up to two parallel workers per gather. No database settings,
   queries or live workloads were stopped or changed in this incident task.
3. Complete nervous-bus-bnti: named container history, process attribution at
   pressure onset with PID-incarnation fencing, and compressed swap physical
   accounting. Existing minute cgroup/GPU history cannot name a process that
   disappeared before inspection. The new signal recorder is bounded local
   evidence, not yet a Nervous Bus event integration.
4. Evaluate model idle residency and persistent development workloads against
   actual use. The neural health response already showed generative models
   unloaded; blanket model-unload or stopping the world server is unsupported
   by this evidence. Further upstream Orca registry-lifetime fixes are also
   available but are outside this two-fix backport.

## Private evidence

Owner-only directory: `~/.cache/nervous-bus-recovery/20260920-evening-crashes`.
Key files: `core-metadata.json`, the two Orca scope journals,
`kernel-matches.log`, `last-2h-series.json`, `incident-interval-summary.json`,
`pss-census.json`, `current-memory-after.json`, `memory-incident.png`,
`policy-before.json`, `policy-after.json`, `signal-recorder-validation.json`,
`signal-capture-clock.json`, `orca-signal-events.log`, the four socket
reproduction JSONs, `reproduction-receipts.json`, and build/test logs.
