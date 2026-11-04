"""Bounded adapters for a private replay observation pipeline.

The package deliberately does not locate, read, or retain game media.  A
caller supplies a hash-bound 25B-shaped result and receives only structured
candidate data for 25D and 25E.
"""

from .core import (
    PipelineContractError,
    build_coach_observation_event,
    build_partial_trait_request,
    run_partial_planner,
)

__all__ = [
    "PipelineContractError",
    "build_coach_observation_event",
    "build_partial_trait_request",
    "run_partial_planner",
]
