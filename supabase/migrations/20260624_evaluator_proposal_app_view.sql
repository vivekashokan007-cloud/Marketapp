-- Market Radar: evaluator proposal app compatibility view
-- Design-only companion to the evaluator scaffold.
-- This does not replace ai_branch_proposals yet.

create or replace view public.evaluator_proposals_app_view as
select
  p.id,
  p.job_id,
  p.status,
  coalesce(p.proposal_payload->>'category', p.title, 'proposal') as category,
  coalesce((p.proposal_payload->>'priority')::integer, p.priority) as priority,
  coalesce(
    p.proposal_payload->>'validation_notes',
    p.proposal_payload->>'validationNotes',
    ''
  ) as validation_notes,
  case
    when p.status = 'approved' then p.updated_at
    else null
  end as approved_at,
  coalesce(
    p.proposal_payload->>'index_key',
    p.proposal_payload->>'index',
    'ALL'
  ) as index_key,
  jsonb_build_object(
    'proposal_id', coalesce(p.proposal_payload->>'proposal_id', p.id::text),
    'status', p.status,
    'index', coalesce(p.proposal_payload->>'index', p.proposal_payload->>'index_key', 'ALL'),
    'index_key', coalesce(p.proposal_payload->>'index_key', p.proposal_payload->>'index', 'ALL'),
    'category', coalesce(p.proposal_payload->>'category', p.title, 'proposal'),
    'priority', coalesce((p.proposal_payload->>'priority')::integer, p.priority),
    'hypothesis', coalesce(p.proposal_payload->>'hypothesis', ''),
    'explanation', coalesce(p.proposal_payload->>'explanation', p.rationale, ''),
    'conditions', coalesce(p.proposal_payload->'conditions', '{}'::jsonb),
    'action', coalesce(p.proposal_payload->'action', '{}'::jsonb),
    'evidence', coalesce(p.proposal_payload->'evidence', '{}'::jsonb)
  ) as proposal_json
from public.evaluator_proposals p;

comment on view public.evaluator_proposals_app_view is
  'Compatibility view shaped for MarketVivi normalizeProposalRow().';
