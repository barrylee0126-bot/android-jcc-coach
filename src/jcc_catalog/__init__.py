"""Local, candidate-only access to game reference snapshots (no network or writes)."""

from .catalog import Catalog, CatalogError, FieldRef, SourceSpec, load_manifest

__all__ = ["Catalog", "CatalogError", "FieldRef", "SourceSpec", "load_manifest"]
