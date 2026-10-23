# RUNBOOK: Bridge UDP transport - live verification

For the orchestrator, in a supervised machine window.
The worker lane never drives FL; this document is the deliverable that lets a human (or the orchestrator) do it.
Design under test: docs/22. Code: `native/bridge/src/bridge_udp.*`, the executor seam in `bridge_core.*`, client `mcp-server/src/fl_mcp/bridge_wire.py`.

Every step states the command, the expected output, and what to conclude if it differs.

## 0. Preconditions

```bash
cd <repo root>
cmake -S native -B native/build -DCMAKE_BUILD_TYPE=Release
cmake --build native/build --target bridge bridge_core_test bridge_udp_test
ctest --test-dir native/build --output-on-failure
native/scripts/install_bridge.sh
```

Expected: both tests pass; install script reports the dylib copied.
If ctest fails: stop - do not test live with a red mock-host suite.

Shell setup used by every step below:

```bash
D="$HOME/Documents/Image-Line/FL Studio/Settings/Piano roll scripts/FLMCP"
PY=mcp-server/.venv/bin/python
```

## 1. Insert and confirm the transport is up

1. Start FL Studio 2026, insert **FL MCP Bridge** on any mixer insert (BRIDGE_FINDINGS.md one-insert procedure).
2. Confirm:

```bash
cat "$D/bridge_status.txt"      # expect: status=loaded
cat "$D/bridge_heartbeat.txt"   # tick=N advancing; NEW: udp_port=<nonzero>, udp_dropped=0
cat "$D/bridge_udp.json"        # {"proto": 1, "transport": 1, "port": <same>, "token": "<32 hex>", "pid": <FL pid>}
```

Decision tree:

- `udp_port=0` in the heartbeat and no `bridge_udp.json` -> UDP failed to start (no urandom / no writable root); the plugin is serving the file spool only. File a finding; every file-spool test still applies.
- `bridge_udp.json` present but heartbeat not advancing -> the Bridge's `Idle_Public` tick is not running; UDP datagrams will queue but never execute. The ping test below MUST time out - that is the designed behavior, verify it does.
- Both present and advancing -> proceed.

## 2. Liveness: ping round trip

```bash
$PY - <<'EOF'
from fl_mcp import bridge_wire
endpoint = bridge_wire.discover()
print("endpoint:", endpoint)
wire = bridge_wire.BridgeWire(endpoint)
print("ping:", wire.ping(timeout=2.0))
EOF
```

Expected: the endpoint prints with the port from step 1 and `ping: True`.

- `endpoint: None` -> the server and plugin disagree on the spool root; compare `bridge_udp.json`'s location against `flpaths.spool_roots()`.
- `ping: False` with FL responsive -> socket thread or queue wedged; capture `bridge_heartbeat.txt` and `bridge_udp.json`, then run step 6's flood check to see whether datagrams are being counted at all.

## 3. Latency: UDP round trip vs the file spool

Ping RTT measures the full pipeline (socket thread -> queue -> tick -> executor -> ack) without touching FL's API, so it is the clean transport number:

```bash
$PY - <<'EOF'
import time
from fl_mcp import bridge_wire
wire = bridge_wire.BridgeWire(bridge_wire.discover())
samples = []
for _ in range(100):
    t0 = time.perf_counter()
    assert wire.ping(timeout=2.0)
    samples.append((time.perf_counter() - t0) * 1000)
samples.sort()
print(f"UDP rtt ms: p50={samples[50]:.1f} p95={samples[95]:.1f} max={samples[-1]:.1f}")
EOF
```

Expected: p50 in the low tens of milliseconds - the floor is FL's tick cadence (~20 ms per tick, the bridge polls every 3rd tick), NOT the network. Anything under ~100 ms p50 proves the transport adds nothing material.

File-spool comparison (same machine, same FL session) - send the same v2 command through the `Spool` class and time until the response file answers:

```bash
$PY - <<'EOF'
import time
from fl_mcp.spool import Spool
spool = Spool()
samples = []
for _ in range(20):
    t0 = time.perf_counter()
    cmd = spool.send("ping")
    spool.await_response(cmd, timeout=10.0)
    samples.append((time.perf_counter() - t0) * 1000)
samples.sort()
print(f"file rtt ms: p50={samples[10]:.1f} max={samples[-1]:.1f}")
EOF
```

Record both numbers in the verdict. Expected: UDP p50 <= file p50 (both are tick-bound; UDP removes the filesystem polling and write amplification, the win grows under load and on slower disks).

## 4. Note write over UDP (the v2 command, end to end)

Both carriers speak the same v2 protocol through one executor, so this is the plain client path:

```bash
# Open a piano roll on the target channel first.
$PY - <<'EOF'
from fl_mcp import bridge_wire
wire = bridge_wire.BridgeWire(bridge_wire.discover())
cmd = wire.send(
    "add_notes",
    target={"pattern": 1, "channel": 0},
    notes=[{"key": 60, "start": 0, "length": 1, "velocity": 0.8},
           {"key": 64, "start": 1, "length": 1},
           {"key": 67, "start": 2, "length": 1}],
)
print("command id:", cmd.id)
print(wire.await_response(cmd, timeout=3.0))
EOF
```

Expected: a `SpoolResponse` with `ok=True, op='add_notes', notes_written=3`, the notes visible in the piano roll with no click or trigger, `"$D/consumed.json"` containing the printed id, and `"$D/responses/<id>.json"` holding the same payload the ack carried.
The response summary will carry a PPQ WARNING if the command had no ppq stamp - `bridge_wire` sends whatever the caller passes, and the session stamps ppq on its own sends; a warning here is expected for this hand-built command and is not a failure.

Then idempotency, live: re-send the SAME id raw (the client never reuses ids on its own):

```bash
$PY - <<'EOF'
import json, socket
from fl_mcp import bridge_wire
endpoint = bridge_wire.discover()
cid = int(input("id printed by the previous step: "))
cmd = {"proto": 2, "id": cid, "op": "add_notes",
       "args": {"target": {"pattern": 1, "channel": 0}, "mode": "merge",
                 "notes": [{"key": 60, "start": 0, "length": 1}]}}
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.connect(("127.0.0.1", endpoint.port))
sock.settimeout(3.0)
sock.send(bridge_wire.MAGIC + endpoint.token.encode() + b" " + json.dumps(cmd).encode())
print(sock.recv(65536).decode())
EOF
```

Expected: an ack for the same id again (the recorded response or a `"duplicate": true` ack), and NO second chord in the piano roll.

## 5. Soak: 1000 commands

```bash
$PY - <<'EOF'
import time
from fl_mcp import bridge_wire
wire = bridge_wire.BridgeWire(bridge_wire.discover())
t0, failures = time.perf_counter(), 0
for i in range(1000):
    if not wire.ping(timeout=5.0):
        failures += 1
print(f"1000 pings in {time.perf_counter()-t0:.1f}s, failures={failures}")
EOF
cat "$D/bridge_heartbeat.txt"
```

Expected: `failures=0`, `udp_dropped=0` (pings are sequential - the queue never fills), FL's UI responsive throughout (drag a window, scroll the playlist DURING the run).
Any failure or UI freeze: capture heartbeat + `Console.app` crash log if FL died, and stop.

## 6. Wedge test: a stopped peer must cost FL nothing

The failure-containment claim (docs/22 s2): FL must never hang or crash because our socket peer is wedged.

```bash
# a) Flood the bridge, then freeze the client mid-stream.
$PY - <<'EOF'
import json, os, signal, socket, time
from fl_mcp import bridge_wire
endpoint = bridge_wire.discover()
frame = lambda p: bridge_wire.MAGIC + endpoint.token.encode() + b" " + json.dumps(p).encode()
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.connect(("127.0.0.1", endpoint.port))
# 5000 pings as fast as the kernel accepts them, never reading a single ack.
for i in range(5000):
    sock.send(frame({"proto": 1, "id": 820000 + i, "op": "ping"}))
print("flooded; now freezing this process for 10s (simulates a SIGSTOPped server)")
os.kill(os.getpid(), signal.SIGSTOP)   # resume from another shell: kill -CONT <pid>
EOF
```

From a second shell, while the flooder is stopped:

```bash
cat "$D/bridge_heartbeat.txt"    # tick still advancing, udp_dropped may be large - that is the design
# FL responsiveness check: interact with FL's UI for 15 seconds.
kill -CONT <flooder pid>
```

Expected:

- FL stays fully responsive during the flood AND while the peer is frozen (acks to a stopped process just drop - non-blocking sendto, nobody waits).
- `tick=` keeps advancing; `udp_dropped` counts the overflow (queue is 64, drain is 4/tick - most of the 5000 drop, by design).
- After `kill -CONT`, a fresh `wire.ping()` from a new process returns True: the transport recovered without any restart.

Any FL freeze or crash here is a P0 finding against the transport - capture everything and stop.

## 7. Clean shutdown

Remove the plugin from the mixer insert (or close FL):

```bash
cat "$D/bridge_status.txt"   # status=destroyed (on removal) - FL did not hang doing it
ls "$D/bridge_udp.json"      # No such file - the port file is unpublished
```

Then re-insert and confirm step 1 passes again on a fresh ephemeral port (it must differ only in port/token).

- Port file still present after removal -> the shutdown path did not run or unlink failed; check status and file a finding.
- FL beachballs on removal for more than ~1 s -> the join-deadline failed its contract; P0, capture a spindump.

## Verdict template

```
UDP-1 transport up          PASS/FAIL   port=<N> token published, heartbeat udp_port matches
UDP-2 ping round trip       PASS/FAIL   <output>
UDP-3 latency               p50/p95 UDP=<..>ms file=<..>ms
UDP-4 note write + idem     PASS/FAIL   notes visible, no double apply
UDP-5 soak 1000             PASS/FAIL   failures=<n> udp_dropped=<n>
UDP-6 wedge                 PASS/FAIL   FL responsive: <observed>
UDP-7 shutdown              PASS/FAIL   status=destroyed, port file gone, reinsert ok
```
