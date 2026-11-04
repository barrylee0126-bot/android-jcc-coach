# Trait vision v1

`jcc_trait_vision` is an offline, single-frame observation adapter for a
declared trait-rail row and the displayed population fraction. It is deliberately
smaller than a board recognizer: it has no tablet input, persistence, catalog
lookup, strategy, or action output.

## Input contract

`probe_trait_frame` accepts image bytes, `source_kind` (`replay` or
`synthetic`), a `FrameRecord`, an optional `PopulationInput`, declared
`TraitRowInput` values, a local OCR backend, and the expected SHA-256 of the
exact encoded input. `FrameRecord` records a case/frame identifier, decoded PTS
and source-video digest. Those identifiers, plus subject and scene, are caller
declarations: the module binds them to the supplied image digest but cannot
authenticate their provenance.

A trait row declares a containing ROI and separate label and ratio ROIs. The
field evidence has to reproduce the input digest, source kind, image geometry,
field identifier and ROI. Missing layout/visibility/animation witness, an
occlusion, source mismatch, unsupported image, OCR failure, an edge-touching
box, multiple boxes, malformed display string or scale disagreement returns
`unknown`.

The regular crop mode requires its box to be one pixel inside the crop. The
optional `full_frame` mode runs OCR over the supplied frame, retaining exactly
one detection box strictly inside the declared source-coordinate ROI. It exists
for backends whose crop-coordinate boxes are unstable; it does not relax the
source ROI or permit a crossing box. Slight overlap of the declared label and
ratio ROIs is allowed solely to retain one-pixel margins around adjacent
vertically stacked text; the accepted OCR boxes remain independently interior.

Population reuses `jcc_capacity_probe` and therefore requires that module's
independent hash/ROI/evidence contract too.

## Output contract

The serializable result contains:

```text
schema_version, implementation_revision,
source_frame {case_id, frame_id, decoded_pts_s, source_video_sha256,
              encoded_sha256, subject, scene, validity,
              context_authority="caller_declared_unverified"},
status, population, trait_rows, accuracy=null, automatic_action=null
```

`population` is either its own `development_candidate` with
`displayed_deployed`, `capacity`, and `empty_slots`, or `unknown` with reason
codes. A trait row exposes separate `label_status` and `ratio_status` alongside
the aggregate row `status`. It can retain a stable display-label candidate while
the numeric field is unknown, but the aggregate row then remains `unknown`.
A stable numeric field can also be retained independently while its label is
unknown, but the aggregate row then remains `unknown`. Every row keeps
`trait_identity="unbound"` because a displayed label has not been reconciled
with a versioned game catalog.

All candidate results are `evidence_status=machine_candidate` in product
presentation, not verified truth. `minimum_raw_score` is only an OCR-output
filter and is not a calibrated probability or an accuracy measure. The first
private replay experiment uses `0.3` solely because this backend reports the
small Chinese ratio and large population glyphs at that raw score; it does not
relax source/geometry/variant gates and must not be reported as recognition
accuracy.

## Explicit non-results

This adapter cannot determine the player, preparation legality, bench unit
identity, complete board, shop, equipment, trait activation, version identity,
or a best move. `subject`, `scene`, and `validity` are visible in output so a
consumer can withdraw rather than convert an unverified caller declaration into
an instruction. A display label plus a numeric candidate is only an observation
until a separate version binding, scene/ownership evidence, independent review,
and product policy admit it.

## Versioned gray dual-ROI ratio candidate

`probe_gray_dual_ratio` is an additional path; it does not replace the original
row reader or convert that reader's `unknown` result into a success. The v1
layout requires exactly two predeclared ROIs with equal width and height:
`up_1px` and `down_1px`, whose vertical origins differ by two pixels. Each view
is nearest-neighbour 2x enlarged, converted to grayscale, auto-contrasted, and
sent to the same local OCR backend. Both must yield exactly one fully interior
box, meet the raw OCR filter, strictly parse under `parse_trait_ratio`, have
identical ratios, and have inverse-projected source boxes with IoU at least the
predeclared threshold (v1 default `0.98`).

The path rejects split boxes, `2./3`, `2/ 3`, OCR failures, visibility or
animation uncertainty, disagreement, low overlap, and source/evidence mismatch.
It returns the two source ROIs, inverse boxes, IoU, preprocessing version and
failure codes. The views are related transformations of the same image by the
same backend, so their agreement is explicitly labelled
`same_frame_same_backend_not_independent_review`; it is neither an independent
vote nor accuracy evidence. As with the base path, `0.3` is only a raw OCR
filter, never a calibrated confidence or verified recognition rate.

The v1 `0.98` IoU is a deliberately strict engineering policy (at least 98% of
the two inverse-projected box areas must overlap), not a threshold learned from
labels. It was informed by a development-only geometry probe, so it is not an
independently pre-registered model-selection rule. Do not lower it merely to
increase candidate count: any replacement threshold must be fixed before an
independent holdout run and reported with its coverage and failures.
