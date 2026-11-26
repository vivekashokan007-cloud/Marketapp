# BNF live capture — active implementation, 26 November 2026

## One source delivery path

Upstox/reference response -> atomic gzip retry file -> existing `SupabaseClient` -> existing `mr_market_source_captures` -> owner-scoped readback -> local acknowledgement.

The raw capture directory is excluded from private checkpoint upload. Recovery of Paper positions/preferences continues separately. No new tables were created for this completion batch. The existing `ml_option_chain_snapshots` price evidence writer now retains instrument key, receipt time, market-data JSON and broker Greeks JSON. Brain snapshot semantics and Paper decision rules are unchanged.

## Input inventory

| Input | Implemented acquisition | Persistence |
|---|---|---|
| Bank Nifty spot, Nifty 50 external context, India VIX | V3 full quotes each watch poll; observational full WebSocket feed | Source rows; VIX remains external context |
| All currently listed BNF option expiries | Union of daily instrument master and option-contract listing; chain per expiry; V3 quotes in 250-key batches and Greeks in 50-key batches | Raw original bytes, all fields, request parameters, receipt clocks |
| OI, prior OI, depth, quantities, CAS fields | Full V3 response preserved | Source rows; broker units unchanged |
| Max pain, dedicated OI/change-OI/PCR | Per expiry, preserving HTTP 200/data null | Source rows and explicit coverage status |
| BNF futures | Current NSE master discovers unexpired futures and exact keys/expiry/lot/tick/freeze metadata; V3 full quotes | Master evidence and source rows |
| Every current bank constituent | Official NSE membership CSV determines keys; no fixed four-bank capture list | Membership source and V3 quote rows |
| Monthly bank weights | Daily publication check discovers monthly ZIP from NSE site; retains original publication; Android PDF extractor validates all current members, sum and as-of date | Versioned source records plus dated local cache; receipt time never replaced by as-of date |
| Daily instrument master | Stream BOD gzip, retain complete BNF/member objects and decoded-master digest | Master source row; no assumption that master and listing are equal |
| Exchange session context | NSE/NFO status and dated market timings each watch poll; WebSocket market-info messages | Source responses retain vendor fields and local receipt time |
| FII/DII | During watch polls plus hourly network-constrained after-close maintenance | Original response and receipt clocks; no backdating |
| WebSocket V3 | Official pinned proto, full mode for context instruments/futures and four near-spot front-expiry options | Original base64 frames, SHA, decoded messages, receipt times; malformed frames retained; reconnect/gap events |
| Coverage | Master/listing/chain omissions, batch counts, null analytics, expected subscription keys | Source-table coverage records |

## Operational acceptance

A successful build does not certify phone coverage. Renew the Upstox token and sign in to the app account before open. Check source-upload status: verified/pending rows and check time. On the live phone, verify three spaced polls, decoded exchange clocks, REST/WebSocket parity, background survival, and a controlled restart. Read source bytes back from Supabase after the session and verify original hashes and lengths. Check fresh NSE publication parsing on the installed Android runtime.

The full WebSocket stream is a bounded observational subscription; all listed options are captured through REST rather than subscribing the phone to every option tick. Android WorkManager is opportunistic, not an exact-time server schedule. An abrupt process death can lose an in-memory WebSocket batch (up to the 15-second flush interval); process/reconnect events flag possible gaps. The phone must run watch mode for continuous session acquisition. Raw local files are retained after acknowledgement; sustained storage/reclamation needs measured session volume before any cleanup policy is enabled.

## Source provenance

- Upstox official proto: https://assets.upstox.com/feed/market-data-feed/v3/MarketDataFeed.proto
- Proto SHA-256: 3e1c939dce2c83a3fef91405c5ca268dfa089aae973319f56685f12a5afcf612
- Master: https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz
- Constituents: https://www.niftyindices.com/IndexConstituent/ind_niftybanklist.csv
- Monthly publication directory: https://www.niftyindices.com/reports/monthly-reports
- Verified fixture: October 2026 ZIP, NIFTY_Bank_Oct2026.pdf; all 14 rows, as of 30 October 2026.

The superseded candidate run 45861445999 was cancelled. Focused capture CI builds no APK. Request a single final candidate only after validation and review of the completed source.
