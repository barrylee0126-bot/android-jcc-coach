package com.android.jcc.coachoverlay;

import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;

/** Fixed display-only envelope; strict parsing avoids Android JSONObject coercion. */
final class OverlayEnvelope {
    static final String SCHEMA_V1 = "tablet-overlay-envelope-v1";
    static final String SCHEMA_V2 = "tablet-overlay-envelope-v2";
    static final String SCHEMA_V3 = "tablet-overlay-envelope-v3";
    private static final Pattern ID = Pattern.compile("[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}");
    private static final Set<String> WITHDRAWALS = Set.of(
            "unknown", "stale", "conflict", "scene_changed", "disconnected", "expired");

    final String streamId;
    final String sessionId;
    final long deliverySequence;
    final long revision;
    final String renderToken;
    final String status;
    final long remainingMs;
    final long leaseMs;
    final long expiresAtElapsedMs;
    final Content content;

    private OverlayEnvelope(String streamId, String sessionId, long deliverySequence, long revision,
                            String renderToken, String status, long remainingMs, long leaseMs, long expiresAtElapsedMs,
                            Content content) {
        this.streamId = streamId;
        this.sessionId = sessionId;
        this.deliverySequence = deliverySequence;
        this.revision = revision;
        this.renderToken = renderToken;
        this.status = status;
        this.remainingMs = remainingMs;
        this.leaseMs = leaseMs;
        this.expiresAtElapsedMs = expiresAtElapsedMs;
        this.content = content;
    }

    static OverlayEnvelope parse(String raw) throws StrictJson.ParseException {
        Map<String, Object> value = StrictJson.object(raw);
        String schema = string(value, "schema_version");
        if (SCHEMA_V1.equals(schema)) return parseV1(value);
        if (SCHEMA_V2.equals(schema)) return parseV2(value);
        if (SCHEMA_V3.equals(schema)) return parseV3(value);
        throw new StrictJson.ParseException("schema");
    }

    private static OverlayEnvelope parseV1(Map<String, Object> value) throws StrictJson.ParseException {
        requireKeys(value, Set.of("schema_version", "stream_id", "session_id", "delivery_sequence",
                "revision", "render_token", "status", "visible", "remaining_ms", "lease_ms", "advice"));
        Parsed base = base(value);
        Object advice = value.get("advice");
        if ("candidate_visible".equals(base.status)) {
            if (!base.visible || !(advice instanceof Map<?, ?>)) throw new StrictJson.ParseException("candidate_shape");
            @SuppressWarnings("unchecked") Map<String, Object> object = (Map<String, Object>) advice;
            return new OverlayEnvelope(base.streamId, base.sessionId, base.deliverySequence, base.revision,
                    base.renderToken, base.status, base.remainingMs, base.leaseMs, 0L, Content.advice(object));
        }
        if (!WITHDRAWALS.contains(base.status) || base.visible || advice != null) throw new StrictJson.ParseException("withdrawal_shape");
        return new OverlayEnvelope(base.streamId, base.sessionId, base.deliverySequence, base.revision,
                base.renderToken, base.status, base.remainingMs, base.leaseMs, 0L, null);
    }

    private static OverlayEnvelope parseV2(Map<String, Object> value) throws StrictJson.ParseException {
        requireKeys(value, Set.of("schema_version", "stream_id", "session_id", "delivery_sequence",
                "revision", "render_token", "status", "visible", "remaining_ms", "lease_ms", "expires_at_elapsed_ms", "source_kind",
                "source_sequence", "frame_id", "context_version", "evidence_id", "evidence_status", "content_kind", "content"));
        Parsed base = base(value);
        String contentKind = string(value, "content_kind");
        Object rawContent = value.get("content");
        if ("candidate_visible".equals(base.status)) {
            if (!base.visible || !(rawContent instanceof Map<?, ?>) || !("advice".equals(contentKind) || "observation".equals(contentKind))) {
                throw new StrictJson.ParseException("candidate_shape");
            }
            identifier(string(value, "source_kind"));
            positive(value, "source_sequence", Long.MAX_VALUE);
            identifier(string(value, "frame_id"));
            long deadline = positive(value, "expires_at_elapsed_ms", Long.MAX_VALUE);
            String context = nullableIdentifier(value, "context_version");
            String evidence = nullableIdentifier(value, "evidence_id");
            identifier(string(value, "evidence_status"));
            @SuppressWarnings("unchecked") Map<String, Object> object = (Map<String, Object>) rawContent;
            Content content = Content.v2(contentKind, object);
            if ("advice".equals(contentKind) && (context == null || evidence == null)) {
                throw new StrictJson.ParseException("advice_evidence");
            }
            return new OverlayEnvelope(base.streamId, base.sessionId, base.deliverySequence, base.revision,
                    base.renderToken, base.status, base.remainingMs, base.leaseMs, deadline, content);
        }
        if (!WITHDRAWALS.contains(base.status) || base.visible || rawContent != null || !"none".equals(contentKind)
                || value.get("expires_at_elapsed_ms") != null
                || value.get("source_kind") != null || value.get("source_sequence") != null || value.get("frame_id") != null
                || value.get("context_version") != null || value.get("evidence_id") != null || value.get("evidence_status") != null) {
            throw new StrictJson.ParseException("withdrawal_shape");
        }
        return new OverlayEnvelope(base.streamId, base.sessionId, base.deliverySequence, base.revision,
                base.renderToken, base.status, base.remainingMs, base.leaseMs, 0L, null);
    }

    /** V3 separates the renderer's bridge revision from the upstream 25E revision. */
    private static OverlayEnvelope parseV3(Map<String, Object> value) throws StrictJson.ParseException {
        requireKeys(value, Set.of("schema_version", "stream_id", "session_id", "delivery_sequence",
                "bridge_revision", "source_revision", "render_token", "status", "visible", "remaining_ms", "lease_ms", "expires_at_elapsed_ms", "source_kind",
                "source_sequence", "frame_id", "context_version", "evidence_id", "evidence_status", "content_kind", "content"));
        String stream = identifier(string(value, "stream_id"));
        String session = identifier(string(value, "session_id"));
        long delivery = positive(value, "delivery_sequence", Long.MAX_VALUE);
        long bridgeRevision = positive(value, "bridge_revision", Long.MAX_VALUE);
        Object rawSourceRevision = value.get("source_revision");
        String token = identifier(string(value, "render_token"));
        String status = string(value, "status");
        boolean visible = bool(value, "visible");
        long remaining = positive(value, "remaining_ms", 10_000L);
        long lease = positive(value, "lease_ms", 800L);
        String contentKind = string(value, "content_kind");
        Object rawContent = value.get("content");
        if ("candidate_visible".equals(status)) {
            if (!visible || !(rawContent instanceof Map<?, ?>) || !"observation".equals(contentKind)) throw new StrictJson.ParseException("candidate_shape");
            positive(value, "source_revision", Long.MAX_VALUE);
            long deadline = positive(value, "expires_at_elapsed_ms", Long.MAX_VALUE);
            identifier(string(value, "source_kind"));
            positive(value, "source_sequence", Long.MAX_VALUE);
            identifier(string(value, "frame_id"));
            nullableIdentifier(value, "context_version");
            nullableIdentifier(value, "evidence_id");
            identifier(string(value, "evidence_status"));
            @SuppressWarnings("unchecked") Map<String, Object> object = (Map<String, Object>) rawContent;
            return new OverlayEnvelope(stream, session, delivery, bridgeRevision, token, status, remaining, lease,
                    deadline, Content.v2(contentKind, object));
        }
        if (!WITHDRAWALS.contains(status) || visible || rawContent != null || !"none".equals(contentKind)
                || (rawSourceRevision != null && !(rawSourceRevision instanceof Long))
                || value.get("expires_at_elapsed_ms") != null || value.get("source_kind") != null || value.get("source_sequence") != null
                || value.get("frame_id") != null || value.get("context_version") != null || value.get("evidence_id") != null || value.get("evidence_status") != null) {
            throw new StrictJson.ParseException("withdrawal_shape");
        }
        return new OverlayEnvelope(stream, session, delivery, bridgeRevision, token, status, remaining, lease, 0L, null);
    }

    String semanticKey() {
        if (content == null) return status + "\u0000withdrawn";
        return status + "\u0000" + content.kind + "\u0000" + content.titleZh + "\u0000" + content.primaryZh + "\u0000" + content.detailZh;
    }

    private static Parsed base(Map<String, Object> value) throws StrictJson.ParseException {
        return new Parsed(identifier(string(value, "stream_id")), identifier(string(value, "session_id")),
                positive(value, "delivery_sequence", Long.MAX_VALUE), positive(value, "revision", Long.MAX_VALUE),
                identifier(string(value, "render_token")), string(value, "status"), bool(value, "visible"),
                positive(value, "remaining_ms", 10_000L), positive(value, "lease_ms", 800L));
    }

    private static final class Parsed {
        final String streamId, sessionId, renderToken, status;
        final long deliverySequence, revision, remainingMs, leaseMs;
        final boolean visible;
        Parsed(String streamId, String sessionId, long deliverySequence, long revision, String renderToken,
               String status, boolean visible, long remainingMs, long leaseMs) {
            this.streamId = streamId; this.sessionId = sessionId; this.deliverySequence = deliverySequence;
            this.revision = revision; this.renderToken = renderToken; this.status = status; this.visible = visible;
            this.remainingMs = remainingMs; this.leaseMs = leaseMs;
        }
    }

    private static long positive(Map<String, Object> value, String key, long maximum) throws StrictJson.ParseException {
        Object raw = value.get(key);
        if (!(raw instanceof Long)) throw new StrictJson.ParseException(key + "_type");
        long number = (Long) raw;
        if (number < 1 || number > maximum) throw new StrictJson.ParseException(key + "_range");
        return number;
    }

    private static long nonnegative(Map<String, Object> value, String key, long maximum) throws StrictJson.ParseException {
        Object raw = value.get(key);
        if (!(raw instanceof Long)) throw new StrictJson.ParseException(key + "_type");
        long number = (Long) raw;
        if (number < 0 || number > maximum) throw new StrictJson.ParseException(key + "_range");
        return number;
    }

    private static String string(Map<String, Object> value, String key) throws StrictJson.ParseException {
        Object raw = value.get(key);
        if (!(raw instanceof String)) throw new StrictJson.ParseException(key + "_type");
        return (String) raw;
    }

    private static boolean bool(Map<String, Object> value, String key) throws StrictJson.ParseException {
        Object raw = value.get(key);
        if (!(raw instanceof Boolean)) throw new StrictJson.ParseException(key + "_type");
        return (Boolean) raw;
    }

    private static String identifier(String value) throws StrictJson.ParseException {
        if (!ID.matcher(value).matches()) throw new StrictJson.ParseException("identifier");
        return value;
    }

    private static String nullableIdentifier(Map<String, Object> value, String key) throws StrictJson.ParseException {
        Object raw = value.get(key);
        if (raw == null) return null;
        if (!(raw instanceof String)) throw new StrictJson.ParseException(key + "_type");
        return identifier((String) raw);
    }

    private static void requireKeys(Map<String, Object> value, Set<String> expected) throws StrictJson.ParseException {
        if (!value.keySet().equals(expected)) throw new StrictJson.ParseException("keys");
    }

    static final class Content {
        final String kind, titleZh, primaryZh, detailZh;
        private Content(String kind, String titleZh, String primaryZh, String detailZh) {
            this.kind = kind; this.titleZh = titleZh; this.primaryZh = primaryZh; this.detailZh = detailZh;
        }
        static Content advice(Map<String, Object> value) throws StrictJson.ParseException {
            requireKeys(value, Set.of("title_zh", "action_zh", "reason_zh"));
            return new Content("advice", text(string(value, "title_zh"), 18), text(string(value, "action_zh"), 28),
                    text(string(value, "reason_zh"), 32));
        }
        static Content v2(String kind, Map<String, Object> value) throws StrictJson.ParseException {
            requireKeys(value, Set.of("title_zh", "primary_zh", "detail_zh"));
            return new Content(kind, text(string(value, "title_zh"), 18), text(string(value, "primary_zh"), 28),
                    text(string(value, "detail_zh"), 32));
        }
        private static String text(String value, int maximum) throws StrictJson.ParseException {
            if (value.isBlank() || value.length() > maximum || !value.equals(value.trim())
                    || value.chars().anyMatch(character -> character < 32)) throw new StrictJson.ParseException("text");
            return value;
        }
    }
}
