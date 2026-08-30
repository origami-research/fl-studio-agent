# The FLP reverse-engineering playbook

This is the repeatable process that decoded the note format, the playlist (all four record eras, including the undocumented 80-byte one), the automation curves, and the cut-window field - written down so that when FL changes the format again, the next person reruns the loop instead of rediscovering it.
Every technique below earned its place by catching a real error in this repo's history; citations point at the evidence.

## The loop

1. **Collect ground truth.**
   FL ships ~164 of its own projects (demos + templates) under the app bundle's `FL/Data` - real files spanning many format eras, free.
   For new-era questions, have FL author fresh saves itself (the corpus harness and `tests/e2e/` matrix already do this where a live window is available).

2. **Byte-diff minimal pairs.**
   Save two FL-authored files differing by exactly one user-visible change; the byte diff localizes the field.
   This is the primary tool for NEW fields.

3. **Prove the skeleton by full-stream consistency.**
   FLP is an event stream; one wrong event size desyncs everything after it.
   A size hypothesis is proven when it makes EVERY corpus file walk from first byte to exactly the last.
   Precedent: event 172 is 1 byte, not the 4 the classic range rule implies - only that assignment walks all FL-2026 saves cleanly (docs/21 addendum 13; flpkit `_EVENT_SIZE_OVERRIDES`).

4. **Treat prior art as hypotheses, never truth.**
   pyflp's structs are the field-name map; verify each claim at corpus scale.
   It has been wrong three times here: event 172, the 80/88-byte playlist strides, and the cut window (its f32 "offsets" are u32 ticks).
   Where it parses correctly, field-by-field differential comparison at scale is a powerful cross-check (the corpus tests encode this, with evidenced divergence classes where the oracle is provably wrong).

5. **Detect structure by signature, never by version list.**
   A constant present in every record (playlist `pattern_base == 20480`) doubles as validator and structure detector: the true record stride is the smallest size that places the signature correctly in every record.
   Future growth then decodes automatically - correct by construction (doctrine #54).

6. **Decode everything, bound everything.**
   Run the decoder over the whole corpus; every field must land in a sane range (track 1..500, normalized values 0..1, positions under ~50M ticks).
   One insane value falsifies the hypothesis.
   Scale matters: 27,010 clips and 9,676 automation points made the current readers trustworthy.

7. **Reading can lie; writing cannot.**
   The "looks right" trap: u32 1536 reads as an f32 denormal of ~0.0, which is how the cut window hid from everyone.
   Test hypotheses by writing bytes per the theory and watching FL's behavior.

8. **Real FL is the only judge - the acceptance ladder.**
   (a) FL opens the written file without complaint.
   (b) FL's observable behavior changes exactly as predicted (live wire readback: e.g. song length grew by precisely the added ticks).
   (c) Strongest: FL's own re-save round-trips the written record byte-identically - FL fully re-serializes on save, so survival means FL understood the bytes, and any rewrite FL makes is a message about which field is wrong (the cut-window bug was found because FL rewrote our head length to 1).

9. **Template, don't invent.**
   New records copy an FL-authored record and patch only understood fields; opaque tail bytes keep FL's defaults.
   A file with no template record is an error, not an invitation to fabricate (flpkit `write_playlist`).

10. **Record falsifications.**
    Every disproven belief becomes a docs/21 addendum and an evidenced test (never a weakened one).
    A falsified founding fact is a result.

## When the format changes again

Symptoms arrive as corpus-test failures or reader warnings, never crashes (the never-crash contract): walker desync, "fits no record size (new era?)", signature misses.
Then: collect fresh saves from the new FL -> full-stream consistency for new event sizes -> signature scan for new strides -> byte-diff minimal pairs for new fields -> sanity-bound at corpus scale -> live acceptance ladder -> record the delta as an addendum and a corpus divergence class if the oracle broke too.

## Where things live

- Reader/writers and evidence comments: flpkit (github.com/clarkipeng/flpkit), one comment per hard-won constant.
- Corpus differential + divergence classes: `mcp-server/tests/test_flp_corpus.py`.
- Live acceptance harness: `experiments/` (guarded input rules) and `tests/e2e/` tier 2.
- History of falsified beliefs: docs/21 addenda 12-16.
