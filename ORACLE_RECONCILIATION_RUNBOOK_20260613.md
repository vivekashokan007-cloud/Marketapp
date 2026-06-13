# Oracle Reconciliation Runbook — 2026-06-13

This runbook executes `DIRECTIVE_OPENCLAW_ORACLE_RECONCILIATION_20260613`.

## Step 0: local expected hash

Run locally in repo:

```bash
cd /root/.openclaw/Marketapp
python3 -c "import hashlib;print(hashlib.sha256(open('oracle_server/evaluator_app.py','rb').read()).hexdigest()[:16])"
```

Record this as `EXPECTED_HASH`.

## Step 1: pull deployed file from VM

On the VM:

```bash
cd ~/market-relay
cp evaluator_app.py evaluator_app.py.pre_reconcile_20260613
```

Copy the deployed file back for diff:

```bash
scp -i /path/to/key opc@144.24.117.114:~/market-relay/evaluator_app.py /tmp/deployed_evaluator_app.py
```

## Step 2: diff deployed vs repo

Run locally:

```bash
diff -u /tmp/deployed_evaluator_app.py /root/.openclaw/Marketapp/oracle_server/evaluator_app.py
```

Classify each hunk as:
- `STALE`
- `DRIFT`
- `SECRET`

If any `DRIFT` changes elephant behavior, stop and review before redeploy.

## Step 3: deploy repo file to VM

Copy repo file:

```bash
scp -i /path/to/key /root/.openclaw/Marketapp/oracle_server/evaluator_app.py opc@144.24.117.114:~/market-relay/evaluator_app.py
```

Restart:

```bash
ssh -i /path/to/key opc@144.24.117.114
cd ~/market-relay
sudo systemctl restart market-relay
sudo systemctl status market-relay --no-pager
```

## Step 4: prove deployed == repo

Check health:

```bash
curl -s https://marketradar-oracle.online/health
```

Must show:
- `deploy_hash == EXPECTED_HASH`
- `prompt_version == "qualitative_prompt_v2"`

## Step 5: manual /elephant probe

```bash
curl -s -X POST https://marketradar-oracle.online/elephant \
  -H 'Content-Type: application/json' \
  -d '{
    "poll_timestamp":"2026-06-13T10:00:00+00:00",
    "session_date":"2026-06-13",
    "lane":"NF_intraday",
    "observe_only":true,
    "quality_tag":"qualitative_prompt_v2",
    "trade_mode":"intraday",
    "decision_source":"VM_PROBE",
    "market_context":{"vix":15.0,"bnf_spot":56805,"nf_spot":23631},
    "verdict_context":{"action":"WAIT","strategy":null},
    "signal_independence":{"summary":"probe"},
    "coherence_signal":{"label":"Signals aligned","impact":"bullish","strength":3},
    "candidate_counts":{"generated":1,"watchlist":1},
    "candidates":[
      {
        "candidate_id":"probe-1",
        "rank":1,
        "lane":"NF_intraday",
        "index":"NF",
        "strategy_type":"BULL_PUT",
        "trade_mode":"intraday",
        "watchlist_rank":1,
        "economics":{},
        "structure":{},
        "execution":{},
        "ml_overlay":{}
      }
    ]
  }'
```

Expected immediate response:
- `202 Accepted`

## Step 6: Supabase verification

Query `elephant_assessments` for the probe `poll_timestamp` and `lane`.

Confirm:
- `distribution_signal ∈ {genuine, hedging, ambiguous, unclear}`
- `coherence_read ∈ {aligned, conflicted, unclear}`
- `candidate_notes[*].stance ∈ {neutral, caution, ignore}`
- no `support`

