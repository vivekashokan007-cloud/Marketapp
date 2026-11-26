# BNF cloud recovery — implementation checkpoint, 26 November 2026

Owner requested that the next update preserve all required application data in Supabase and recover it on another phone. b542 does not satisfy this requirement. Do not install or describe b543 as accepted until the remaining acceptance checks below pass.

## Implemented on the review branch

- Native app-account authentication, independent of dashboard authentication, with refresh credentials encrypted by Android Keystore in noBackupFilesDir. Broker tokens, sandbox credentials, AuthAccess credentials, process leases and diagnostic logs are excluded from portable backups. No service-role credentials are shipped.
- A dedicated private `mr-private-state` bucket, account-owned checkpoint heads and an immutable checkpoint history index. Restrictive Storage policies protect the new bucket even when older broad permissive policies exist. The existing EXPORTS bucket and trading tables are unchanged.
- Immutable, compressed 1 MiB chunks, SHA-256 verification, complete object readback before compare-and-swap checkpoint commit, and head readback before reporting success. Failed uploads retain local data and the previous cloud checkpoint.
- Native files, typed SharedPreferences, trained models, open and closed Paper state, research/session evidence, outboxes, and PWA localStorage are covered by the recovery manifest. Credentials require a fresh login on a new device.
- PWA state is restored before trading boot callbacks and synchronously captured after storage mutations. The first upgrade preserves existing DOM storage until native migration captures it. A visible banner distinguishes verified checkpoints, pending changes and capture failures.
- Full closed-trade keyset pagination replaces the 200-row recovery limit. Existing NF history remains recoverable; BNF-only admission remains unchanged.
- A new installation hydrates from its app account before monitoring. A server-owned 120-second device lease and revision checks prevent two updated installs from acting as competing managed writers. Automatic work pauses after lease expiry; manual Paper cache remains explicitly available without claiming backup readiness. Old pre-cloud app versions do not participate in this lease and must be stopped during a phone transfer.
- Save-before-trim queues preserve native full-snapshot/brain/research evictions and common Python durable-store replacements. Queue bytes are removed locally only after they appear in a verified cloud checkpoint; the server checkpoint-history index retains the object references. This is distinct from a market-data capture quota: data never collected by a pre-existing quota is not manufactured.
- Whole-state recovery verifies all objects in staging before activation. A persistent activation marker gates services after interruption; credentials and process leases are not replayed. Unknown/newer formats and local/cloud conflicts stop automatic recovery.

The app uses local working caches and a durable upload queue for offline operation. It does not run its Python brain inside Supabase. No claim of zero-loss is made for unsent data if a phone is physically lost before upload. Cloud state is acknowledged only after verified readback.

## Checks completed locally

- JVM protocol suite: six tests pass, including 351 closed trades, multi-chunk research, interruptions, corrupted/missing objects, unsafe paths, downgrade, secret exclusion, and save-before-trim acknowledgement.
- PWA bootstrap: first upgrade, cloud restore, empty cloud state, 351 trades, storage mutations, failed capture and status pass.
- Python archive: two tests pass, including retaining the original when the history queue cannot be written.
- Existing single-leg Python regressions: 62 tests pass.
- PostgreSQL 17 scratch fixture: anonymous isolation despite broad existing anon policy, two-account isolation despite broad authenticated policy, immutable objects, conditional revisions, competing devices and expired lease recovery pass. No production data is used.

## Remaining acceptance work

1. Finish Android compilation and complete JVM suite; run lifecycle/restore integration checks, including interrupted activation and ambiguous commit response.
2. Review save-before-discard coverage and account/credential exclusions against actual on-device inventory. Legacy data not present on the phone or already in Supabase cannot be reconstructed.
3. Apply the additive migration only after the database tests pass on the final SQL and confirm actual project access policies. Verify deployed schema/RLS and private bucket access. Do not alter the public EXPORTS bucket, existing trade policies or Auth settings incidentally.
4. Build the signed candidate from pinned native and PWA commits through the existing owner-review environment. Do not publish main/Pages or an automatic release as part of this review.
5. On the original phone, sign in/create the application account and verify the first native+PWA checkpoint. Verify a separate fresh-install restore against this real inventory before claiming phone migration is proven. Keep the owner's export. Dashboard sign-in is not application authentication.

The already verified managed database backup from 26 November 2026 05:24:37 IST protects database contents. It does not include Storage object bodies. This feature's private Storage checkpoint is therefore not an independent backup against losing the entire Supabase project; separate disaster-recovery object copies remain necessary.

The predictive brain objective, BNF-only scope, single-leg buying priority, later protected two-leg selling research, manual Paper availability and Real disabled are unchanged. This is persistence infrastructure, not evidence of a trading edge.
