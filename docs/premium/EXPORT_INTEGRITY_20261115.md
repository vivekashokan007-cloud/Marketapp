# Premium export integrity — the offline verifier, and a correction (15 November 2026)

**The 12 November 2026 phone export is intact.** All 1,603 digests in it recompute exactly:
641 records, 107 envelopes, 107 acquisition ids, 107 cohort refs, 641 envelope refs.

An earlier version of this document said the opposite — 0 of 107 envelopes — and it was wrong.
The correction is recorded here rather than quietly replaced, because the mistake is cheap to
repeat and the repository had already written down why.

## What went wrong, exactly

`PremiumObservationContract.canonicalJson` delegates string escaping to `JSONObject.quote`.
**That is not the same function on Android as on the JVM.** This project found the same divergence
once before and documented it in `Pc2CompactBatch`, round 3:

> Android's org.json escapes every `/` as `\/` and leaves U+0080–U+009F and U+2000–U+20FF
> literal. The reference org.json used by the JVM tests escapes `/` only after `<`, and escapes
> U+0080–U+009F and U+2000–U+20FF as `\uXXXX`.

Premium capture still delegates. So a phone export's digests cover **Android-escaped** bytes,
while any JVM or desktop verifier recomputes over **reference-escaped** bytes. Every premium
string containing a slash — `lot.contract_lot_table_ref`, for one — then hashes differently off
the device. The 12 November bundle contains **648 `\/` sequences**: the divergence is visible in
the artifact itself.

My error was not the hypothesis — I tested "string encoding or escaping" and cleared it. It was
the reference: I compiled `PremiumObservationContract` against `org.json:json:20240303`, got the
same digest as the Python mirror, and called that "Android's own encoder". It is not. Compiling
the contract on a JVM establishes agreement **under the json implementation that run used**, and
nothing more. Records verified under both rules only because the record body happens to carry no
slash; the envelope's lot reference does.

Credit where due: Codex found it by changing one thing — escaping every slash — and reproducing
the stored digest exactly.

## The verifier, as it now stands

`tools/premium_export_verify.py` recomputes every digest an export carries, under an explicit
**encoding profile**:

| profile | string rule | used for |
| --- | --- | --- |
| `android-legacy` | escape `"` `\` and **every** `/`; five short forms; other < U+0020 as lowercase `\u00xx`; everything else literal | v1 artifacts produced on the phone |
| `desktop` | the reference org.json 20240303 rule | v1 artifacts produced by a JVM run |
| `explicit-v2` | the v2 rule below | an undeclared body known to have been written under it |

The **v1** `hash_basis.serialization` tag is **shared** between the first two — both say
`json_sorted_keys_compact_utf8_v1` — so it cannot select the profile. What can is the provenance
the export already carries: `evidence_context.source_kind` of `LIVE_DEVICE_CAPTURE` means
Android's org.json; anything synthetic or replayed means a JVM run. `--profile` overrides it, and
**the decision and its reason are printed** rather than assumed. One profile is applied to every
v1 digest in an artifact.

A **v2** body needs none of that: it declares
`json_sorted_keys_compact_utf8_v2_explicit_escaping`, a rule that is the same on every runtime, so
it is always recomputed under that rule whatever profile was selected or passed — and the report
states how many bodies are self-describing and how many the profile governs. That is also what
lets one artifact hold both, which an in-place upgrade mid-session produces.

When the selected profile mismatches, the report adds a **diagnostic** naming the profile that
does reproduce the digests. It is a diagnostic only: the verdict and exit status stay with the
selected profile, because a verifier that retries until something passes verifies nothing.

```
$ python3 -I tools/premium_export_verify.py mr_session_evidence_2026-11-12.json
encoding profile android-legacy   (provenance: LIVE_DEVICE_CAPTURE, so Android's org.json)
  -- NF --   record_payload VERIFIED=318 | envelope_payload VERIFIED=53 | acquisition_id
             VERIFIED=53 | cohort_ref VERIFIED=53 | envelope_ref VERIFIED=318
VERDICT: ALL 1603 RECOMPUTED DIGESTS MATCH (profile android-legacy)
```

### Exit statuses, documented for automation

| code | meaning |
| --- | --- |
| 0 | `VERIFIED` — every digest checked matched |
| 1 | `MISMATCH` — a digest is present and does not match the bytes it covers |
| 2 | `UNREADABLE` — unreadable, or no premium section |
| 3 | `NOTHING_TO_VERIFY` — a premium section with no records and no envelopes |
| 4 | `INCOMPLETE` — a required binding is absent, so verification could not complete |
| 5 | `PROFILE_UNUSABLE` — the artifact's own declarations conflict or name a rule this verifier does not implement, no profile was given, **and** at least one body depends on that choice. Nothing is recomputed |

Precedence across files and indices: **1 > 2 > 5 > 4 > 3 > 0**. An older export missing a binding
reports 4, never 1 — absent is not wrong, and only present-and-wrong fails. An empty export is
**not** 0: 13 November has nothing to recompute and must not read like a day that was checked.
Exit 5 exists because guessing an encoding invents the verdict: a copy of the 12 November export
with NIFTY declaring Android and BANKNIFTY declaring the JVM rule used to warn, pick one and exit
0. It now recomputes nothing. If every body in such an artifact declares v2, no profile is
consulted, so it verifies and the conflicting declaration is reported rather than being fatal.

**Run it before the eligibility checks**, not after: there is no point scoring an artifact whose
digests have not been shown to cover its bytes.

```
python3 -I tools/premium_export_verify.py <export.json>   # integrity first
python3 -I preflight_export.py <export.json>              # then the R7 capture preflight
```

The frozen R7 package and the eligibility and scoring rules are untouched.

**Monday, 16 November, pass the profile explicitly.** The installed signed b530
(`398e1ab2250c4cdbe97e83c78f0b13795493c9df`) predates `canonical_quote_impl` — that field landed
four commits later, in `4052c3e` — so its export declares no quoter and selection would fall back
to provenance. For a known Android build, say so:

```
python3 -I tools/premium_export_verify.py --profile android-legacy \
    mr_session_evidence_2026-11-16.json
```

## Still true, and still worth keeping

- **A tamper is still caught.** Changing one underlying spot value in a disposable copy fails
  that envelope's `payload_sha256` and its `acquisition_id`, and the six records bound to that
  acquisition lose their `envelope_ref` — while the other 106 envelopes and all 24 records in the
  fixture still verify. The Android profile is not a rule that makes everything pass.
- **A self-reported counter is not verification.** `accounting.envelope_hash_verified` happened
  to be right here, but it is a number the producer writes about itself; the point of an outside
  verifier is that it does not have to be believed.
- **The paired fixtures stay paired.** A known-good JVM artifact sits beside the phone one, so a
  verifier that failed everything would be caught — which is exactly the failure mode that
  produced the wrong conclusion above.

## The byte budget, measured as the device stores it

Two things made the earlier measurement understate this: toy clocks (`1000`/`1300` instead of
13-digit epoch milliseconds), and measuring JVM bytes when the phone stores Android-escaped bytes,
which are one byte larger per slash. The cap is enforced on **stored** bytes.

**From the real 12 November export, all 107 stored envelopes: 2,012 to 2,021 bytes** against the
2,048 B cap — the fleet ran on **27 to 36 bytes** of headroom. The largest is BNF 13:45 at
2,021 B.

Through the real producer with production clocks (`jvm → android stored`):

| configuration | jvm | android | headroom |
| --- | ---: | ---: | ---: |
| NF weekly, `map_ref` null | 1,991 | 1,994 | 54 |
| BNF monthly, `map_ref` null | 2,001 | 2,004 | 44 |
| NF weekly, `map_ref` a dated path | 2,014 | 2,018 | 30 |
| BNF monthly, `map_ref` a content reference | 2,031 | 2,034 | **14** |
| the same plus a vendor timestamp | — | — | **REFUSED BY THE STORE** |

That last line is an overflow **reached on the current build**, with a payload the contract
accepts. An overflow is not a degraded envelope: `putEnvelope` is refused and, by the fail-closed
rule, every observation in that slot is dropped — the 8 November incident (`a69bfd0`) again.

### The smallest change that fixes it

`lot.lot_authority_requires` costs **106 bytes** per envelope and has **one distinct value in the
entire export** — a fixed policy string
(`RESOLVER_VALIDATED_TABLE_ROW_FOR_THIS_UNDERLYING_EFFECTIVE_AT_THE_SESSION_DATE`) repeated 107
times, already implied by `production_authority_ref`, which names the per-session authority record
that the string describes. Moving it onto that record takes the real worst case from 2,021 B to
about 1,915 B, and the both-additions case to about 1,968 B, with nothing lost. This is the move
that fixed the same problem before: take content out by reference rather than enlarge the cap.

**Applied** on `review/claude-premium-evidence-20261115`: the constant now lives on the authority
record as `authority_requires`, the envelope is `mr.premium_slot_envelope.v4`, v3 stays supported,
and the payload the store refused outright now stores at 1,952 B with 96 B to spare. Measured
through the real producer, 106 B recovered per envelope — exactly the size of the constant that
moved. Next-build, so Monday's capture still runs on the installed b530.

## The app-side fix, applied

The verifier's `android-legacy` profile reads existing exports. It did not remove the cause: the
contract delegated to `JSONObject.quote`, so the device and every off-device verifier disagreed by
construction.

The cause is now removed, as `Pc2CompactBatch` removed it for PC2 telemetry in round 3. The rule
is written out and implemented without delegation, in the Kotlin and in the committed Python
mirror the Kotlin's vectors are asserted against:

> escape `"` and `\`; the short forms `\b \f \n \r \t`; every other character below U+0020 as
> lowercase `\u00xx`; lone surrogates as lowercase `\uXXXX`; everything else literal, **including
> `/` and all non-ASCII**.

`CANONICAL_SERIALIZATION` becomes `json_sorted_keys_compact_utf8_v2_explicit_escaping`, and it is
declared inside `hash_basis`, which each body's own digest covers — so relabelling a body to
borrow another encoder breaks its digest instead of changing what verifies.

**Nothing already stored is rewritten.** `canonicalJsonV1` is kept verbatim, and every
verification path — `verifyHash`, `contentHash`, `acquisitionId`, `batchEvidenceRef` — now reads
the rule the body it is checking DECLARES instead of assuming today's. Two of those were
genuinely load-bearing: the acquisition basis contains `lot`, whose table reference carries
slashes, so recomputing a stored id under the new rule would have reported every envelope on the
phone as not derived from its own basis. A real mixed session (committed as
`export_integrity_mixed_v1_v2_fixture.json`: 09:20 and the pinned cohort v1, 09:25 v2, one
export) reports both acquisitions `VERIFIED` with no integrity reasons, and the offline verifier
recomputes all 30 of its digests.

One honest limit: on a JVM the v1 and v2 encodings of a premium body coincide — the characters
they disagree about are not in this evidence — so the character-level proof is on constructed
bodies (`U+0085`, `</`) in both suites, and the proof against real Android bytes remains the
`android-legacy` profile on the 12 November export.
