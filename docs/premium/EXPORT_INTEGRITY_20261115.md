# Premium export integrity — what recomputation actually finds (15 November 2026)

**Short version.** The offline verifier now exists and reads real phone exports. It does **not**
confirm the premise it was built on. In the 12 November 2026 phone export, **641 of 641
observation records verify and 0 of 107 slot envelopes do** — under the independent Python mirror
*and* under Android's own `PremiumObservationContract.verifyHash` run over the exported bytes.
Both implementations produce the **same** recomputed digest, so this is **not** a cross-language
encoding difference.

The export's own `accounting.envelope_hash_verified` says **53** and **54** — all 107 verified on
the device at export time. Recomputation from the file says none of them do. One of those two
statements is about bytes that exist; the other is a counter the producer wrote about itself.

Nothing here changes a frozen rule, and no digest was reinterpreted to make anything pass.

## What was measured, and how

| check | 12 Nov phone export | synthetic export from HEAD's real pipeline |
| --- | --- | --- |
| `record_payload` | **641 / 641 verified** | 828 / 828 verified |
| `envelope_payload` | **0 / 107** verified | 138 / 138 verified |
| `acquisition_id` re-derived from its own basis | **0 / 107** | all verified |
| `cohort_ref` re-derived from the exported cohort | **107 / 107 verified** | all verified |
| `envelope_ref` on each record | 0 / 641 | all verified |
| the phone's own `envelope_hash_verified` | 53 and 54 | — |

Reproduce:

```
python3 -I tools/premium_export_verify.py <export.json>
```

## Hypotheses eliminated, each by measurement rather than reading

1. **A Python-mirror bug.** Ruled out: Android's own `verifyHash`, compiled from
   `PremiumObservationContract.kt` and run over the same exported bytes on a JVM, fails the same
   107 envelopes and returns *byte-identical* recomputed digests to the mirror
   (`f1a669d0719d47bcb8083fc3096470a557ed7e6c3f8980b362baaa6cd4e33bac` for NF 12:20 in both).
2. **A canonical-encoding divergence between Android and desktop.** Ruled out by the same
   result, and positively: the envelope's `cohort_ref` — a `contentHash` over the exported
   cohort, using the identical encoder — **re-derives exactly, 107 times out of 107**. The
   encoder is right, and now proven against real device bytes rather than only against synthetic
   vectors.
3. **The encoding changed after the export was written.** Ruled out:
   `PremiumObservationContract.kt` is unchanged between b528 (`6eca48b`, the build that wrote the
   file — its bundle reports `brain_version 2.6.97`) and HEAD.
4. **A schema difference.** Ruled out: the 12 November envelope and a HEAD envelope have
   identical key sets at every level, including inside `clocks`, `underlying` and `lot`.
5. **Export-time mutation of the hashed object.** Not found: the exporter does
   `envArr.put(it)` — the envelope verbatim — and no code in `export()` mutates an envelope or
   any of its nested objects after the verify.
6. **Number reformatting in transit.** Ruled out: the envelope's numeric literals in the file are
   plain and unremarkable (`1794466205573`, `119`, `25234.2`, `65`), and parse back to exactly
   those values.
7. **String encoding or escaping.** Ruled out: all 31 string values in the envelope are plain
   ASCII, and the two implementations' quoting agrees on every one.
8. **A key added or removed after stamping.** Ruled out by search: no removal of one, two or
   three field paths from the exported envelope reproduces either the claimed
   `payload_sha256` or the claimed `acquisition_id`.
9. **The lot-authority path or a null-valued field.** Ruled out by reproduction: HEAD's real
   coordinator path, with the authority reference attached and `lot.map_ref` JSON `null`,
   produces envelopes that verify in memory, after a text round trip, and after a real export.

## What this narrows it to

`payload_sha256` and `acquisition_id` are the two values derived from the envelope's **mutable
basis** (`clocks`, `underlying`, `lot`, `batch_evidence_ref`, `request_outcome`, `slot`,
`session_date`, `index`). Both fail. `cohort_ref`, the one value **not** derived from that basis,
verifies. So one or more *values* inside that basis differ from what they were when the id and
the digest were computed — not a key, not a number format, not a string encoding.

**I could not identify the specific field from the artifact alone, and I am not going to guess
it.** The remaining untested candidate is `batch_evidence_ref`, a digest over the prepared
six-contract evidence whose array order is not recoverable from the export; confirming or
clearing it needs the producer, not the file.

## Why it matters, stated at its real size

- **Record-level integrity for 12 November is intact and now independently proven**: 641 of 641.
  The quotes the research line reprices are covered by digests that recompute exactly.
- **Envelope-level integrity for that date cannot be re-established offline.** The envelope
  carries the clocks, the underlying spot, the lot basis and the acquisition identity. A replay
  that needs to prove *which acquisition* a quote came from cannot do that from this file by
  digest alone.
- **It is not known to affect any other date.** 12 November is the only phone export in hand that
  contains premium evidence at all: 2, 3 and 4 November carry no premium section, and 13 November
  captured nothing (the 735 B headroom refusal). So this is one observation, not a trend — and
  equally, there is **no** phone export whose envelope digests do verify.
- **HEAD is clean on every test available here**, including a full three-session synthetic run
  (2,070 digests, all matching). That is consistent with the defect being fixed, and consistent
  with it needing a condition my synthetic runs do not reproduce. Those are different
  conclusions and the evidence does not separate them.

## The check that should exist and does not

The export states `envelope_hash_verified` as a count the producer computed about itself. On this
file that number is 53/54 while recomputation gets 0/107, so the field cannot be used as
evidence. A verifier outside the producer is the only thing that settles it — which is what
`tools/premium_export_verify.py` now is, and why the suite pins a **known-good** fixture beside
the failing one: a verifier that fails everything would prove nothing.

## Monday

This does not block collection on b530. If both indices save quotes, record-level integrity is
checkable immediately with the verifier, and the envelope question is answered by running it on
the fresh export: a b530 export whose envelopes verify localizes the 12 November failure to the
older build; one whose envelopes do not reproduces it on a build we control, which is the better
outcome for fixing it.

Run on the fresh export, before any scoring:

```
python3 -I tools/premium_export_verify.py mr_session_evidence_2026-11-16.json
```
