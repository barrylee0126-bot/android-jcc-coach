"""Reproducible offline field evaluation; importing never touches a device."""

from .evaluator import EvaluationInputError, evaluate, render_markdown

__all__ = ["EvaluationInputError", "evaluate", "render_markdown"]
