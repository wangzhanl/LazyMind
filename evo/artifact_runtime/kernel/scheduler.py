from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from .graph import NextOp


ReadyOrder = Literal['topological', 'partition_pipeline']


@dataclass(frozen=True)
class ConcurrencyLimits:
    max_in_flight: int = 1
    per_materializer: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.max_in_flight, int) or isinstance(self.max_in_flight, bool):
            raise TypeError('max_in_flight must be int')
        if self.max_in_flight < 1:
            raise ValueError('max_in_flight must be >= 1')
        limits = dict(self.per_materializer)
        for key, value in limits.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError('per_materializer keys must be non-empty str')
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError('per_materializer values must be int')
            if value < 1:
                raise ValueError('per_materializer values must be >= 1')
        object.__setattr__(self, 'per_materializer', MappingProxyType(limits))


def select_ready_op(
    ready: tuple[NextOp, ...],
    recent: NextOp | None,
    ready_order: ReadyOrder,
) -> NextOp | None:
    # `ready` must already be ordered by the graph's deterministic topological sort.
    if not ready:
        return None
    if ready_order == 'topological' or recent is None or not recent.partition:
        return ready[0]
    for op in ready:
        if op.partition == recent.partition and op.sort_key > recent.sort_key:
            return op
    return ready[0]


__all__ = ['ConcurrencyLimits', 'ReadyOrder', 'select_ready_op']
