# Catalog source-ID converter v1

## Purpose and boundary

`jcc_catalog_pre_adapter` is a local, pre-adapter structural converter. It is
for sources whose own identifiers cannot satisfy `jcc_catalog_snapshot`'s
ASCII key contract. It has no filesystem or network I/O, accepts only
`source.status = "unknown"`, and never creates a source binding, a verified
claim, a game rule, or an action recommendation.

The converter is intentionally not a replacement for the catalog snapshot
adapter. A later, independently evidenced binding process would still be
needed before any record could enter a decision pipeline.

## Request contract

```json
{
  "format_version": "jcc-catalog-pre-adapter-request/v1",
  "source": {"status": "unknown", "snapshot_namespace": "sha256:<64 lowercase hex>"},
  "traits": [{"source_id": "...", "display_name": "...", "thresholds": [2, 4]}],
  "heroes": [{"source_id": "...", "display_name": "...", "trait_source_ids": ["..."]}],
  "items": [{"source_id": "...", "display_name": "...", "component_source_ids": ["..."]}],
  "conflicts": [{"conflict_id": "...", "entity_kind": "trait", "source_id": "...", "field": "..."}]
}
```

Raw IDs must be nonempty trimmed Unicode text with no control/surrogate
characters. They are not inferred from a display name, alias, version, or
other field. Trait thresholds must be nonempty, positive, and strictly
increasing; hero trait references must be nonempty and non-duplicated to match
the downstream snapshot contract. The converter permits a repeated component
reference, but rejects missing references and item recipe cycles.

## Key and privacy contract

`snapshot_namespace` is a caller-declared digest-shaped label for this one
local snapshot. The converter checks only its syntax; it has no raw payload to
verify the digest, origin, or relation to the supplied records. In particular,
the same raw ID from another source, season, snapshot, or mode must be treated
as a different candidate unless a separate evidenced identity-binding process
proves otherwise.

For valid raw IDs, the converter emits:

```
src-<singular-kind>-SHA256(UTF-8(snapshot_namespace + NUL + "<plural-kind>:" + raw_source_id))
```

The key is deterministic and ASCII but opaque. The result carries a reversible
raw-ID/key map strictly so the caller can retain provenance in a private
0700/0600 store. That map, source records, screenshots, icons, device
identifiers, and any real-game input must never be committed or published. A
malformed row that still exposes a valid `source_id` retains that identity only
to detect a duplicate with a complete row; both remain blocked and neither is
consumable.

The code checks duplicate raw IDs separately within each kind and also checks
the theoretically possible canonical-key collision. Neither condition is
resolved by choosing one record. A duplicate or collision remains visible in
`records` but is excluded from the consumable `private_key_map`; that map only
contains one-to-one candidate records.

## Coverage and conflict contract

Every input row appears once in `records.<kind>` as either `candidate` or
`blocked`; each coverage object obeys:

```
input_total = candidate_count + blocked_count
```

Blocked rows retain one or more explicit reasons, including malformed text,
duplicate source IDs, invalid/missing trait thresholds, missing/blocked
references, recipe cycles, and unresolved attached conflicts. A conflict is
kept in the conflict ledger and blocks its attached record; it is never
silently dropped. A partial output must report its full denominator and reason
counts rather than calling the candidate rows a complete catalog. Each blocked
row also has exactly one `primary_block_reason`; the sum of
`primary_block_reason_counts` equals `blocked_count`. `reason_occurrence_counts`
records all reasons and may exceed `blocked_count` because a row can, for
example, have both an invalid threshold and an unresolved conflict. It must
never be presented as a count of unique blocked rows. A malformed individual
row or conflict does not discard valid neighbouring rows: it remains in the
same input denominator as an index-only `invalid_record`/`conflict_invalid`
block. Duplicate conflict IDs are retained as blocked conflict-ledger entries.
When such a duplicate still has a valid explicit entity kind and source ID,
every determinable target is conservatively marked `unresolved_conflict`; the
ambiguous ledger entries are not treated as distinct evidence.

The same conservative attachment applies to a malformed conflict whose ID or
other fields are absent/invalid but whose entity kind and source ID are still
safe and explicit. Reference arrays are normalized only after each element is
confirmed to be safe text; nested arrays or objects yield `reference_invalid`
and never enter graph lookup.

The item component reference limit is eight, matching the downstream snapshot
contract: eight references are structurally eligible, while a ninth remains in
the input denominator with `component_references_too_many` and is not
consumable. A malformed conflict with a still-recognizable `conflict_id` also
participates in duplicate-ID detection, so a complete peer with that same ID
cannot remain `retained_unresolved`.

For item dependencies, actual strongly connected component members are marked
`recipe_cycle`; upstream items that depend on a blocked cycle or another
blocked component receive `reference_blocked` instead. The complete safe graph
is built before missing-reference blocking, so a cycle remains diagnosable even
when one member also has a missing component. Cycle detection and dependency
closure are iterative, supporting the contractual 1024-item chain. The
dependency block is propagated to a fixed point, so the result is independent
of input row order, including the full overlapping-reason ledger for records
already blocked by a separate missing reference.

`status: "candidate"` means only that local structural conversion completed.
The output always carries `source_status: "unknown"`,
`decision_eligible: false`, and `action_advice: null`. It cannot establish
that a source, hash, version, mode, region, patch, client, or current match is
authentic or applicable.
