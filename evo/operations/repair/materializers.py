from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable

from .loop import build_verified_patch, prepare_candidate_workspace, run_repair_loop
from .plan import build_repair_plan


def repair_materializers() -> dict[str, Callable[[Any, Mapping[str, object]], Mapping[str, object]]]:
    def plan(ctx: Any, inputs: Mapping[str, object]) -> Mapping[str, object]:
        return {'plan': build_repair_plan(_mapping(inputs['analysis'], 'analysis'),
                                          _mapping(inputs['policy'], 'policy'))}

    def workspace(ctx: Any, inputs: Mapping[str, object]) -> Mapping[str, object]:
        return {'workspace': prepare_candidate_workspace(_mapping(inputs['plan'], 'plan'),
                                                         _mapping(inputs['policy'], 'policy'))}

    def loop(ctx: Any, inputs: Mapping[str, object]) -> Mapping[str, object]:
        cases, judges = inputs.get('cases'), inputs.get('baseline_judges')
        if not isinstance(cases, tuple):
            raise ValueError('repair.loop_result cases input must be a partitioned tuple')
        if not isinstance(judges, tuple):
            raise ValueError('repair.loop_result baseline_judges input must be a partitioned tuple')
        return {'result': run_repair_loop(
            _mapping(inputs['workspace'], 'workspace'),
            cases,
            judges,
            _mapping(inputs['eval_policy'], 'eval_policy'),
            _mapping(inputs['candidate_config'], 'candidate_config'),
            _mapping(inputs['policy'], 'policy'),
            ctx,
        )}

    def verified(ctx: Any, inputs: Mapping[str, object]) -> Mapping[str, object]:
        return {'patch': build_verified_patch(_mapping(inputs['loop'], 'loop'))}

    return {
        'repair.plan': plan,
        'repair.candidate_workspace': workspace,
        'repair.loop_result': loop,
        'repair.verified_patch': verified,
    }


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f'{name} must be a mapping')
    return value
