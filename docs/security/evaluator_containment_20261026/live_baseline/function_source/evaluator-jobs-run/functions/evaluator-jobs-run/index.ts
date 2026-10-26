import { adminClient } from "../_shared/client.ts";
import { json, okOptions } from "../_shared/cors.ts";

const AGGREGATES_DEFERRED_REASON =
  "teacher ruler defective (A1/A2/A3); outcome aggregates not computed until corrected teacher_config_version and 5 clean sessions exist";

const KNOWN_EXCLUDED_SESSIONS = [
  {
    session_date: "2026-06-12",
    reason: "100_percent_null_r_junk_session",
  },
  {
    session_date: "2026-06-15",
    reason: "100_percent_null_r_junk_session",
  },
];

const DEFECT_REGISTER = [
  {
    id: "A1",
    title: "teacher_stop_loss_policy_mismatch",
    status: "open",
    finding:
      "Teacher SL is 1.0x maxLoss while displayed/traded policy is 0.6x; every r_multiple/SL-rate describes a policy nobody trades.",
  },
  {
    id: "A2",
    title: "credit_stop_strike_breach_artifact",
    status: "open",
    finding:
      "Credit stops cannot fire until short-strike breach; EOD/SL distribution is partly an artifact.",
  },
  {
    id: "A3",
    title: "time_basis_calendar_vs_trading_days",
    status: "open",
    finding:
      "Time basis uses calendar-days/252 and overstates time by about 42%, distorting probability/ranking.",
  },
  {
    id: "A4",
    title: "tp_horizon_intraday_unreachable",
    status: "open",
    finding:
      "50% TP intraday horizon was effectively unreachable in the audit: 1 of 264 hit; DTE cliff 0 of 1,363 legs >=13 DTE.",
  },
  {
    id: "A5",
    title: "orphaned_outcome_rows",
    status: "open",
    finding:
      "6,177 ml_evaluation_outcomes rows have null session_date; they are recoverable via snapshot_id but not in scope for this scaffold.",
  },
  {
    id: "A6",
    title: "junk_null_r_sessions",
    status: "open",
    finding:
      "2026-06-12 and 2026-06-15 are known June 100% null-R junk sessions and must be excluded.",
  },
  {
    id: "A7",
    title: "clean_session_count_too_low",
    status: "open",
    finding:
      "Only a small clean-session base exists; promotion requires at least 5 clean sessions under corrected teacher_config_version.",
  },
  {
    id: "A8",
    title: "legacy_win_rate_lie",
    status: "open",
    finding:
      "Legacy win-rate summaries diverge from honest teacher evidence; ml_daily_accuracy is excluded from evaluator evidence.",
  },
  {
    id: "A9",
    title: "ranking_inversion_status",
    status: "deferred",
    finding:
      "Ranking-inversion findings remain hypotheses pending corrected teacher ruler and remeasurement.",
  },
  {
    id: "A10",
    title: "daily_teacher_reports_local_only",
    status: "deferred",
    finding:
      "Daily teacher research reports are app-local and not a Supabase evidence source for the monthly evaluator.",
  },
  {
    id: "A11",
    title: "gemini_unlock_gate",
    status: "open",
    finding:
      "Gemini remains locked until A1/A2/A3 are resolved/decided, 5 clean corrected sessions exist, and the 264-outcome remeasurement is complete.",
  },
] as const;

type SupabaseClient = ReturnType<typeof adminClient>;

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function asString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function asPositiveInteger(value: unknown, fallback: number): number {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : fallback;
}

function parseIndexScope(value: unknown): string[] {
  if (Array.isArray(value)) {
    const cleaned = value
      .map((item) => String(item || "").trim())
      .filter(Boolean);
    return cleaned.length ? cleaned : ["BNF", "NF"];
  }
  return ["BNF", "NF"];
}

function uniqueSorted(values: Array<string | null | undefined>): string[] {
  return [...new Set(values.filter((value): value is string => Boolean(value)))].sort();
}

function isDateInWindow(date: string, dateFrom: string | null, dateTo: string | null): boolean {
  if (dateFrom && date < dateFrom) return false;
  if (dateTo && date > dateTo) return false;
  return true;
}

async function computeDataQualityPreamble(supabase: SupabaseClient, job: Record<string, unknown>) {
  const dateFrom = asString(job.date_from);
  const dateTo = asString(job.date_to);
  const outcomeQuery = supabase
    .from("ml_evaluation_outcomes")
    .select("session_date,teacher_config_version");

  const boundedOutcomeQuery = dateTo
    ? (dateFrom ? outcomeQuery.gte("session_date", dateFrom).lte("session_date", dateTo) : outcomeQuery.lte("session_date", dateTo))
    : (dateFrom ? outcomeQuery.gte("session_date", dateFrom) : outcomeQuery);

  const { data, error } = await boundedOutcomeQuery.limit(10000);
  const excludedSessions = KNOWN_EXCLUDED_SESSIONS.filter((session) =>
    isDateInWindow(session.session_date, dateFrom, dateTo)
  );

  let orphanedRowCount: number | null = null;
  let orphanedRowStatus = "not_checked";
  const { count: orphanCount, error: orphanError } = await supabase
    .from("ml_evaluation_outcomes")
    .select("id", { count: "exact", head: true })
    .is("session_date", null);
  if (orphanError) {
    orphanedRowStatus = `check_failed:${orphanError.message}`;
  } else {
    orphanedRowCount = orphanCount ?? 0;
    orphanedRowStatus = orphanedRowCount > 0
      ? "present_recoverable_not_in_scope"
      : "none_found";
  }

  if (error) {
    return {
      status: "metadata_query_failed",
      error: error.message,
      clean_session_count: null,
      clean_sessions: [],
      excluded_sessions: excludedSessions,
      orphaned_outcome_rows: {
        count: orphanedRowCount,
        status: orphanedRowStatus,
      },
      teacher_config_versions: [],
      teacher_config_version_current: null,
    };
  }

  const rows = Array.isArray(data) ? data as Array<Record<string, unknown>> : [];
  const excludedDateSet = new Set(excludedSessions.map((session) => session.session_date));
  const sessionDates = uniqueSorted(rows.map((row) => asString(row.session_date)));
  const cleanSessions = sessionDates.filter((date) => !excludedDateSet.has(date));
  const teacherConfigVersions = uniqueSorted(rows.map((row) => asString(row.teacher_config_version)));

  return {
    status: "metadata_only",
    window: {
      date_from: dateFrom,
      date_to: dateTo,
    },
    clean_session_count: cleanSessions.length,
    clean_sessions: cleanSessions,
    excluded_sessions: excludedSessions,
    orphaned_outcome_rows: {
      count: orphanedRowCount,
      status: orphanedRowStatus,
    },
    teacher_config_versions: teacherConfigVersions,
    teacher_config_version_current: teacherConfigVersions.at(-1) ?? null,
    note: "Outcome aggregates are intentionally not computed in this scaffold.",
  };
}

function summarizeWindow(job: Record<string, unknown>, runAttempt: number, dataQualityPreamble: Record<string, unknown>) {
  return {
    date_from: job.date_from,
    date_to: job.date_to,
    run_attempt: runAttempt,
    advisory_only: true,
    source: "supabase_edge_scaffold",
    aggregates_status: "DEFERRED",
    aggregates_deferred_reason: AGGREGATES_DEFERRED_REASON,
    data_quality_status: dataQualityPreamble.status,
  };
}

function buildBriefArtifact(
  jobId: string,
  job: Record<string, unknown>,
  runAttempt: number,
  dataQualityPreamble: Record<string, unknown>,
) {
  const requestPayload = asRecord(job.request_payload);
  return {
    job_id: jobId,
    run_attempt: runAttempt,
    artifact_kind: "prompt_bundle_scaffold",
    prompt_version: "brief_v1_scaffold_no_aggregates_20260704",
    input_payload: {
      schema_version: "brief_v1",
      scaffold_version: "20260704_run_attempt_data_quality_preamble",
      job: {
        job_id: jobId,
        run_attempt: runAttempt,
        date_from: job.date_from,
        date_to: job.date_to,
        index_scope: parseIndexScope(job.index_scope),
        mode: requestPayload.mode || "manual_advisory",
        model_name: job.model_name,
        status_before_run: job.status,
      },
      data_quality_preamble: dataQualityPreamble,
      defect_register: DEFECT_REGISTER,
      aggregates: null,
      aggregates_status: "DEFERRED",
      aggregates_deferred_reason: AGGREGATES_DEFERRED_REASON,
      proposal_task: {
        objective: "propose branch/filter/ranking changes only after corrected teacher evidence unlocks the evaluator",
        guardrails: [
          "no live trade authority",
          "no auto approval",
          "no runtime mutation",
          "no Gemini call in this scaffold",
          "no proposal extraction in this scaffold",
        ],
      },
      stub: {
        note: "A-plus preamble scaffold only. No outcome aggregates, Gemini call, proposals, or live branch writes are performed.",
        request_payload: requestPayload,
      },
    },
    snapshot_count: 0,
    decision_count: 0,
  };
}

function buildVerdictArtifact(jobId: string, job: Record<string, unknown>, runAttempt: number) {
  return {
    job_id: jobId,
    run_attempt: runAttempt,
    verdict_kind: "gemini_response_stub",
    model_name: job.model_name,
    raw_text: "Stub run only. No Gemini call executed.",
    normalized_payload: {
      schema_version: "verdict_v1",
      job_id: jobId,
      run_attempt: runAttempt,
      advisory_only: true,
      summary: {
        proposal_count: 0,
        high_confidence_count: 0,
        notes: [
          "Stub evaluator run only.",
          "No Gemini call executed.",
          "Outcome aggregates are deferred until teacher defects A1/A2/A3 are corrected or decided.",
        ],
      },
      proposals: [],
    },
    token_usage: {},
  };
}

Deno.serve(async (req) => {
  const options = okOptions(req);
  if (options) return options;
  if (req.method !== "POST") return json({ ok: false, error: "Method not allowed" }, { status: 405 });

  let body: Record<string, unknown> = {};
  try {
    body = await req.json();
    const jobId = String(body?.job_id || "").trim();
    if (!jobId) throw new Error("job_id is required");

    const supabase = adminClient();
    const { data: job, error: jobError } = await supabase
      .from("evaluator_jobs")
      .select("*")
      .eq("job_id", jobId)
      .single();
    if (jobError) throw jobError;
    if (job.status === "completed") {
      return json({
        ok: false,
        job_id: jobId,
        status: "completed",
        error: "Job already completed; duplicate evaluator run refused.",
      }, { status: 409 });
    }

    const runAttempt = asPositiveInteger(job.run_attempt, 1) + 1;
    const startedAt = new Date().toISOString();
    const { error: runningError } = await supabase
      .from("evaluator_jobs")
      .update({
        status: "running",
        run_attempt: runAttempt,
        started_at: startedAt,
      })
      .eq("job_id", jobId);
    if (runningError) throw runningError;

    const dataQualityPreamble = await computeDataQualityPreamble(supabase, job);
    const briefArtifact = buildBriefArtifact(jobId, job, runAttempt, dataQualityPreamble);
    const verdictArtifact = buildVerdictArtifact(jobId, job, runAttempt);

    const { error: briefError } = await supabase
      .from("evaluator_brief_artifacts")
      .upsert(briefArtifact, { onConflict: "job_id,run_attempt" });
    if (briefError) throw briefError;

    const { error: verdictError } = await supabase
      .from("evaluator_verdict_artifacts")
      .upsert(verdictArtifact, { onConflict: "job_id,run_attempt" });
    if (verdictError) throw verdictError;

    const { error: updateError } = await supabase
      .from("evaluator_jobs")
      .update({
        status: "completed",
        completed_at: new Date().toISOString(),
        proposal_count: 0,
        summary: summarizeWindow(job, runAttempt, dataQualityPreamble),
        error: null,
      })
      .eq("job_id", jobId);
    if (updateError) throw updateError;

    return json({
      ok: true,
      job_id: jobId,
      status: "completed",
      run_attempt: runAttempt,
      proposal_count: 0,
      message: "Evaluator scaffold run completed without Gemini proposals.",
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    try {
      const jobId = String(body?.job_id || "").trim();
      if (jobId) {
        const supabase = adminClient();
        await supabase
          .from("evaluator_jobs")
          .update({
            status: "failed",
            error: message,
            completed_at: new Date().toISOString(),
          })
          .eq("job_id", jobId);
      }
    } catch (_) {}
    return json({ ok: false, error: message }, { status: 400 });
  }
});
