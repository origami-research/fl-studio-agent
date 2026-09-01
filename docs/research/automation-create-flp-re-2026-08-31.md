# Automation-clip creation FLP RE: no byte contract yet

Status: **no writer and no target-link claim**.  This is a durable negative
result, not a codec design.  `flpkit.write_automation` remains limited to an
existing FL-authored type-5 channel; #107 did not create a channel or link.

## Attempted minimal pair

On 2026-08-31, FL Studio Producer Edition v26.1.5 build 5384 opened a copy of
its `Templates/Minimal/Empty/Empty.flp` as `created.flp`.  The intended one
change was **Create automation clip** for a single visible parameter.  FL
launched, but this shared-desktop session could not keep its native window
frontmost long enough to invoke the parameter context menu.  No clip was
created and therefore no UI mutation was saved.

The attempted pair is deliberately recorded because it falsifies the tempting
claim that merely opening FL produces an evidence pair:

| file | bytes | SHA-256 | diff |
| --- | ---: | --- | --- |
| baseline | 46,361 | `ae67bc831525c0650de212b1e614d9dc541ead54af72a6eb413482d9413685d4` | none |
| created | 46,361 | `ae67bc831525c0650de212b1e614d9dc541ead54af72a6eb413482d9413685d4` | none |

`cmp` returned 0.  A zero-delta pair identifies **no** creation event and
must not be used as a template.

## What the baseline does prove

The FL-authored baseline has a single rack channel, IID 0, with event 64
(`Channel.New`) at FLdt offset 232 and event 21 (`Channel.Type`) at 235.  Its
type byte is not 5, and it contains no event 234 (`Channel.Automation`).  That
is only a starting state: it says the new clip must add or transform channel
state; it says nothing about the record set, target link, channel ID allocation,
or playlist placement.

## Required evidence before implementation

Capture a fresh pair while FL is frontmost, with the same empty template and
one action only:

1. Save the baseline.
2. Right-click one named, stable parameter (recommend `Sampler volume`) and
   choose **Create automation clip**.
3. Save the changed project under a second name, without moving the clip or
   changing selection/name/plugin state.
4. Diff complete FLdt event streams, attribute every changed event, and
   independently parse the result with `flpkit.read()`.
5. Re-open a file written by a proposed `AutomationFormat` creator, verify the
   named target and points in FL, then have FL save it and require byte-identical
   survival.

The creation writer may use only an FL-authored new-clip record as its template
and must preserve every unproven byte.  Until that pair exists, the strict E2E
gate in `test_automation_create_minimal_pair.py` is the executable boundary.
