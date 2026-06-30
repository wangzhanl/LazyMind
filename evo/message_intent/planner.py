from __future__ import annotations

import json
from collections.abc import Mapping
from json import JSONDecodeError
from typing import Any

from pydantic import ValidationError

from evo.llm import LazyLLMClient

from .schemas import TurnPlan


class StructuredPlanError(ValueError):
    pass


def plan_next_turn(context: Mapping[str, Any], llm_config: Mapping[str, Any]) -> TurnPlan:
    schema = TurnPlan.model_json_schema()
    prompt = (
        'You translate one user message into a strict Evo message_intent TurnPlan. '
        'Return only one JSON object. Do not explain. Do not use markdown. '
        'Allowed next_action.kind: flow, query, mutation, config_patch, approval, clarify, final. '
        'For flow/query/mutation/config_patch, set turn_decision to next_action. '
        'Use needs_approval only when deciding an existing pending approval. '
        'Allowed flow command: continue, pause, resume, cancel, retry. '
        'Allowed query: progress_snapshot, read_step_root, read_case_artifact. '
        'Allowed mutation: edit_artifact, rerun_case_stage, rerun_step, invalidate_from_step. '
        'Allowed config_patch target: run_config, source_config, target_config, eval_policy, '
        'repair_policy, candidate_config. '
        'If pending_approval exists, use approval decision approve/reject/amend/replace/unclear, '
        'or output a replacement executable action with user_message_effect amend/replace. '
        'Use schema_version "message_intent.v1". '
        f'TurnPlan JSON schema: {json.dumps(schema, ensure_ascii=False)}\n'
        f'Context: {json.dumps(context, ensure_ascii=False, sort_keys=True, default=str)}'
    )
    llm = LazyLLMClient(llm_config=llm_config, model='evo_llm')
    try:
        raw = _call_structured(llm, prompt, schema)
        data = raw if isinstance(raw, Mapping) else json.loads(str(raw))
        return TurnPlan.model_validate(data)
    except (JSONDecodeError, TypeError, ValidationError) as exc:
        raise StructuredPlanError(str(exc)) from exc


def _call_structured(llm: LazyLLMClient, prompt: str, schema: Mapping[str, Any]) -> Any:
    return llm(prompt, response_format={
        'type': 'json_schema',
        'json_schema': {'name': 'TurnPlan', 'strict': True, 'schema': schema},
    })
