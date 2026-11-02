"""Private replay-only conditional explanation; never emits game actions."""

from .core import ReplayCoachError, build_replay_coach

__all__ = ("ReplayCoachError", "build_replay_coach")
