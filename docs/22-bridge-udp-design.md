# 22 - Bridge UDP transport: design

Workstream B deliverable (2026-08-29).
The Bridge plugin (`native/bridge/`) consumes a file spool polled from FL's extended tick (`Idle_Public`).
File polling caps latency at the poll interval, costs filesystem writes per command, and cannot carry request/response reads.
This document designs a localhost UDP transport inside the plugin, then the implementation follows it.

Doctrine check (issue #54): this adds a transport, it does not add a second implementation of command execution.
There is ONE executor, ONE ledger, ONE result shape; UDP and the file spool are two entry points into them.
The file spool survives deliberately - it is the only path FL's sandboxed pyscript consumer can use, and the fallback for oversized payloads and for a dead socket.

## 1. Threading: what is legal off the host's callback threads

**The FL SDK's own contract** (the licensed SDK's plugin API header, comment block above `TFruityPlug`):

- Every plugin entry point is annotated `(G)` GUI thread, `(M)` mixer thread, `(GM)` both, or `(S)` MIDI-sync thread; `Dispatcher` is `(GM)`, `Idle_Public` and `DestroyObject` are `(G)`.
- `(M)` calls arrive inside the plugin lock, and the header warns: "don't call back to the host while inside a LockPlugin / UnlockPlugin block".
- `FHD_SetThreadSafe` exists so a plugin can declare its own thread-sync - the SDK explicitly contemplates multithreaded plugins.

Nothing in the SDK grants a plugin-created thread the right to call the host.
The tick is `Idle_Public` itself, an SDK callback FL drives from its main run loop (the call contract is in `native/abi/fl_host_abi.h`). It is therefore a `(G)` callback, and the existing spool consumer already issues its `FHD_AddNotesToPR` / `FHD_SetDirty` from exactly there - that precedent is live-verified on this machine.

**Precedent for a background socket thread in audio plugins.** This is common, established practice:

- **ReaStream** (Cockos, bundled with REAPER) is a VST that streams audio and MIDI over UDP (port 58710) directly from the plugin; it has shipped for over a decade (https://www.reaper.fm/reaplugs/).
- **AudioGridder** (https://audiogridder.com, GPL) runs a full network client inside AU/VST3 plugins to offload DSP to a server process; its plugin-side client threads never touch host callbacks - results are handed back to the audio thread through queues.
- **Wwise authoring plugins** (Audiokinetic) talk to the authoring app and the sound engine over sockets from plugin code; Audiokinetic's docs describe the authoring-side connection machinery as ordinary plugin practice (https://www.audiokinetic.com/library/edge/?source=SDK&id=goingfurther_newplugins.html).
- **JUCE**, the dominant plugin framework, ships `juce::Thread`, `StreamingSocket`/`DatagramSocket`, and `InterprocessConnection` precisely for use inside plugins; its documentation and forum guidance are consistent: background threads are fine, but host interaction belongs on the message/audio callbacks (https://juce.com/learn/documentation, class `DatagramSocket`).
- **iPlug2** ships plugin examples with embedded network I/O (e.g. its remote-control/OSC paths) under the same discipline (https://github.com/iPlug2/iPlug2).

**The discipline, distilled** (and enforced by construction in this design):

1. The socket thread NEVER calls the host - not `Dispatcher`, not logging into host state, nothing. It only: `recvfrom`, authenticate, enqueue into a mutex-guarded bounded queue.
2. All host calls (`FHD_AddNotesToPR`, `FHD_SetDirty`) happen only on the tick (`Idle_Public`), which drains the queue - the same place the file spool is already consumed.
3. Ack datagrams are sent from the tick thread with a non-blocking `sendto`; a full socket buffer drops the ack (the client retransmits; the ledger dedupes).
4. The queue is the ONLY shared state between the two threads, and it is a value queue (owned strings) - no pointers into either thread's stack, no host structures cross the boundary.

## 2. Failure containment: FL must never hang or crash because our socket is wedged

Threat model: the socket thread wedges, the server floods us, the server dies mid-stream, or FL destroys the plugin while a datagram is in flight.

- **Bounded queue, drop-oldest.** Capacity 64 datagrams.
  On overflow the OLDEST entry is dropped and counted: newest commands win because a flooding client is retransmitting anyway, and a dropped command simply never acks - the client times out and falls back to the file spool.
  The ledger keeps a drop from ever causing a double apply on retry.
- **The tick does bounded work.** At most `kMaxCommandsPerTick` commands are executed per tick, so a burst cannot turn one tick into an unbounded stall of FL's GUI thread.
- **No blocking syscalls on host threads.** The tick thread's only socket call is non-blocking `sendto` for acks.
  The socket thread is the only place that waits, and it waits in `poll()` with a 200 ms timeout so it re-checks its stop flag even if no traffic ever arrives.
- **Datagram limits.** The receive buffer is 64 KiB (the UDP maximum), so no datagram can be truncated silently; anything that fails the frame check (magic + token) is dropped and counted, never parsed further.
- **Heartbeat.** The existing `bridge_heartbeat.txt` (written every tick) already proves the tick path is alive.
  UDP liveness is proven end to end by the `ping` op: it is answered from the DRAIN (tick) side, not the socket thread, so a pong proves socket thread -> queue -> tick -> ack, i.e. the whole pipeline - not merely that a thread exists.
  A wedged FL (modal dialog, frozen GUI) stops pongs while the port file still exists; the client treats missing pongs as "bridge down, use the file spool".
- **Clean shutdown from `DestroyObject`.** `DestroyObject` is a `(G)` call and `delete this` - the plugin must not block FL's GUI thread waiting on a thread that might be wedged, and the thread must not touch freed memory.
  Sequence: set the atomic stop flag, `close()` the socket, then wait on a condition variable ("thread exited") with a 300 ms deadline; if the thread exited, `join()` it; otherwise `detach()`.
  To make detach SAFE rather than a dangling-pointer gamble, all state the thread touches (socket fd, stop flag, queue, exit cv) lives in a single `std::shared_ptr` block co-owned by the thread; after detach the thread keeps its own reference and exits on its next 200 ms poll cycle, freeing the block itself.
  **Tradeoff, stated:** a detached thread can outlive the plugin by up to one poll interval and is leaked if the process dies first - that is acceptable; blocking FL's GUI thread indefinitely on `join()` is not.
  The port file is removed before the wait, so no new client traffic targets a dying transport.
- **Crash containment.** The socket thread's loop body catches nothing - it has nothing to catch: no allocation beyond `std::string`/`std::vector` (failure raises `std::bad_alloc`, and the existing tick-level `try/catch` in `bridge.cpp` already contains executor-side throws).
  The thread performs no host calls, so a bug there can corrupt at most our own queue, never FL's state.

## 3. Port strategy: ephemeral bind + port file, token auth

**Fixed port rejected.** A fixed port collides with a second FL instance, a second user on the machine, or an unrelated service; "pick another port on EADDRINUSE" reinvents discovery anyway.

**Chosen: bind `127.0.0.1:0` (ephemeral), publish a port file.**
After bind, the plugin writes `bridge_udp.json` atomically (tmp + rename) into the spool roots the server already reads (`Settings/Piano roll scripts/FLMCP`, then the fallback root - `flpaths.SPOOL_SUBDIRS`):

```json
{"proto": 2, "transport": 1, "port": 49731, "token": "9f2c...32 hex...", "pid": 81233}
```

- The server already watches this directory for `responses/` and `consumed.json`, so discovery adds no new location - one settings tree, one source of truth.
- `pid` lets a client distinguish a stale file (bridge crashed, port re-assigned to someone else) cheaply; the authoritative liveness check is a `ping` round trip.
- On clean shutdown the file is unlinked (the plugin is native code - FL's Python sandbox rules do not apply to it).
- Two FL instances: each writes the file on insert; last writer wins, matching the existing single-instance assumption of the spool itself.

**Auth: a 128-bit random token in every datagram.**
Localhost UDP is spoofable by ANY local process (the kernel checks nothing but the address), so every request must prove it read the port file, and every ack must prove it came from the holder of the same token:

- Token: 16 bytes from `/dev/urandom`, hex-encoded (32 chars), generated at transport start.
  If `/dev/urandom` is unavailable the transport does not start (fail closed) - the file spool remains.
- The bridge drops any datagram whose source address is not loopback or whose token does not match (constant-time compare; cheap even though localhost timing attacks are academic).
- The client validates the token on every ack before parsing, so a local process that saw the client's ephemeral port cannot forge results.
- The port file lives in the user's own settings tree with user-only ownership - the token's confidentiality boundary is the OS user account, which is exactly the boundary the file spool already trusts.

## 4. Datagram protocol

**Framing.** One command per datagram, prefix-framed so the socket thread authenticates with a `memcmp` and never parses JSON:

```
FLMCP1 <32-hex token> <payload JSON, verbatim>
```

Acks use the identical frame with the bridge's same token.
The `1` in the magic is the transport version; a future incompatible frame bumps it.

**Payload: the v2 spool command JSON, verbatim.**
The payload octets after the second space are exactly the bytes `spool.py` would have written to `commands/NNNNNN.json` - same `SpoolCommand` model, same ids, same ops.
No re-serialization anywhere: the client sends the canonical encoding, the bridge hands the raw octets to the executor.
One protocol, two carriers (doctrine: one canonical function per rule - the command schema is defined once, in `schemas.py`).

**Acks.** Every accepted datagram gets an explicit ack whose payload is the v2 result shape (`SpoolResponse`-compatible, the same JSON written to `responses/<id>.json`):

```json
{"id": 2007, "ok": true, "op": "add_notes", "notes_written": 3, "backend": "bridge", ...}
```

- Correlation is by `id`, exactly like the file spool and the SysEx wire.
- Acks answer the state the bridge CONFIRMED (dispatcher issued, ledger recorded), never an echo of the request (AGENTS.md: boundaries verify).
- The liveness probe is an ID-LESS `ping`: answered statelessly (`ok`, ack id 0, the bridge's `proto` in the ack) without touching FL's API or the ledger, so heartbeats can never evict real ids from the 64-entry ledger ring.
  A `ping` that CARRIES an id is a queued command like any other and is claimed - in the v2 file spool that claim is what prunes a ping from the queue when the server dies before pruning it (agreed with workstream A, whose consumer relies on it).

**Idempotency and retransmits.** UDP delivers zero or more times, so the client retransmits until acked or deadline.
Safety comes from the SAME consumed-ledger ids as the file spool: a retransmitted id already in the ledger is not re-executed; the bridge re-acks it from a small in-memory result cache (last 64 results), or with a minimal `consumed` ack if the id predates the cache (e.g. it was applied via the file spool by a previous process).
Because both transports share one ledger, the same id can be raced down both paths (client times out on UDP, falls back to the spool, the datagram arrives late) and still applies exactly once.

**Size limit.** UDP caps a datagram at 65,507 payload bytes; loopback carries that reliably, but this transport declares ~60 KB (`kMaxDatagramBytes = 60000`) as its limit to leave frame headroom.
A command larger than the limit is not fragmented at our layer - the CLIENT routes it to the file spool instead (fragmentation-and-reassembly over lossy datagrams is a re-implementation of TCP; the file spool already exists and is the better tool for bulk).
A 5,000-note `add_notes` (schema max) serializes around 600 KB worst case, so bulk writes use the spool; typical musical commands (tens of notes) are a few KB and fit with two orders of magnitude to spare.

**Timeout semantics, stated honestly.** The file spool can cancel a timed-out command (tombstone + queue removal) because the consumer has not read it yet; a UDP datagram already received cannot be uncancelled, so a command that times out client-side may still apply up to one queue-drain later.
The client therefore reports timeouts as retryable failures (same `ToolError` as the spool), and the session layer's existing readback verification - not the transport - remains the authority on what actually landed.
The window is bounded: queue capacity (64) times drain rate (4/tick at FL's ~20 ms tick) is under a second.

## 5. Both transports, one executor

The bridge serves BOTH carriers through one code path:

```
socket thread:  recvfrom -> frame check (magic+token) -> queue.Push(payload, reply_addr)
                                                              |
idle tick:      queue.Drain(4) -> ExecuteCommand(...) --------+--> ack via sendto
                PollOnce()      -> ExecuteCommand(...)             (responses/<id>.json always written)
```

- `bridge_core::ExecuteCommand` is the refactored body of today's `PollOnce`: parse -> ledger check -> claim-before-execute -> dispatch -> result JSON.
  `PollOnce`'s file-reading front half becomes `PollSpoolOnce`, which feeds the SAME `ExecuteCommand` - the monolith is replaced, not duplicated (net structure, not net copies).
- One ledger (`consumed.json`) spans the pyscript, the file spool path, and UDP - exactly-once across all three consumers/carriers, unchanged.
- `responses/<id>.json` is still written for every claimed command regardless of carrier, so `fl_doctor`, humans, and the file-spool server read one place.
- When UDP is down (no port file, no pong, oversized command), the client uses the file spool; nothing about the spool changes.

**Merge seam with workstream A (bridge-v2-sync) - RESOLVED at merge.** This lane confined its refactor to the enqueue/drain seam; A's v2 protocol (PR #57: `queue.json` discovery, per-id command files and tombstones, the ppq stamp, the v2 `SpoolResponse` shape, claim-before-proto-check) merged first and now lives inside `ExecuteCommand`, where both carriers inherit it.
`PollOnce` kept A's discovery half verbatim; the per-id tombstone in `RecordConsumed` is safe cross-carrier by construction (a UDP-carried id can only tombstone its own mirrored file).
The ping rule agreed with A holds in the merged executor: an id-carrying ping claims (queue hygiene), an id-less ping is the stateless heartbeat.

## 6. Test plan

Mock-host (ASan/UBSan, no FL):

- Queue discipline: FIFO, capacity bound, drop-oldest, drop counting, bounded drain.
- Executor equivalence: the same command bytes via the UDP drain and via the file spool produce identical host dispatches, ledger entries, and result fields.
- Datagram failures: wrong magic, wrong token, non-loopback source, oversize, malformed JSON - each dropped/acked per protocol and counted, none reaching the host.
- Idempotency: duplicate datagram re-acks without re-execution; ledger shared with a file-spool apply of the same id.
- Shutdown: `Stop()` joins within its deadline with the socket idle and mid-traffic; port file removed.
- Real-socket round trip: a test client sends `ping` and `add_notes` through a live `UdpTransport` and receives authenticated acks.

Python (`bridge_wire.py`): discovery from the port file, token validation on acks, retransmit-until-ack, timeout raising the spool's own retryable error, oversize routing to the caller's fallback, and a fake bridge responder that speaks the ack shape byte-for-byte.

Live verification is the orchestrator's: `native/bridge/RUNBOOK_BRIDGE_UDP.md` carries the exact procedure (latency vs the file spool, 1000-command soak, SIGSTOP wedge test with FL responsiveness checks).
