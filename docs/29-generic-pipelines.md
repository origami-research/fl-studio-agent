# Generic pipelines

This is the target architecture, now reduced to its durable form. The routing doctrine is [docs/33](33-capability-acquisition-model.md); the executable queue is [docs/26](26-master-plan.md).

A new capability should be data dropped onto an existing pipeline, not a hand-rolled subsystem.

## The five pipelines

| Pipeline | Shape | Capability adds |
|---|---|---|
| Codec | locate -> decode/encode -> splice -> verify-by-readback | a format schema and verifier |
| Transport | frame -> send -> await/drain over a live carrier | a carrier only when the medium is new |
| Router | operation + target + evidence -> first serving lane | an operation spec and capability requirement |
| Discovery | pass spec -> issue commands -> fold evidence into profile | a pass spec |
| Reverse | minimal pair / corpus diff / live acceptance ladder | a bounded experiment and checked-in evidence |

The profile is the single source for version-specific facts. Provenance tags record how each public capability was verified.

## Repository shape

```text
flpkit/
  codec.py
  schema.py
  formats/
  detect.py

fl_mcp/
  core/
    discovery.py
    provenance.py
    router.py
    transport.py
  ops/
    notes.py
    playlist.py
    automation.py
    levels.py
    mixer.py
    params.py
  fl/
    paths.py
    process.py
    install.py

native/
  abi/
  bridge/
```

`flpkit` is pure file codec plus format data. `fl_mcp/core` holds feature-free pipelines. `fl_mcp/ops` says what each capability is. `fl_mcp/fl` quarantines OS/FL coupling.

## Bar

Every change should:

- reduce duplicate code or add only data/schema/spec evidence;
- keep version-specific constants out of feature code;
- leave one readable pipeline module rather than feature-specific branches;
- make readback or byte invariants the acceptance gate;
- update docs/24 and provenance when capability coverage changes.
