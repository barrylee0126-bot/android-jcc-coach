package com.android.jcc.coachoverlay;

import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;

/** Fixed display-only envelope; strict parsing avoids Android JSONObject coercion. */
final class OverlayEnvelope {
    static final String SCHEMA = "tablet-overlay-envelope-v1";
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
    final Advice advice;

    private OverlayEnvelope(String streamId, String sessionId, long deliverySequence, long revision,
                            String renderToken, String status, long remainingMs, long leaseMs,
                            Advice advice) {
        this.streamId = streamId;
        this.sessionId = sessionId;
        this.deliverySequence = deliverySequence;
        this.revision = revision;
        this.renderToken = renderToken;
        this.status = status;
        this.remainingMs = remainingMs;
        this.leaseMs = leaseMs;
        this.advice = advice;
    }

    static OverlayEnvelope parse(String raw) throws StrictJson.ParseException {
        Map<String, Object> value = StrictJson.object(raw);
        requireKeys(value, Set.of("schema_version", "stream_id", "session_id", "delivery_sequence",
                "revision", "render_token", "status", "visible", "remaining_ms", "lease_ms", "advice"));
        if (!SCHEMA.equals(string(value, "schema_version"))) throw new StrictJson.ParseException("schema");
        String streamId = identifier(string(value, "stream_id"));
        String sessionId = identifier(string(value, "session_id"));
        String renderToken = identifier(string(value, "render_token"));
        long deliverySequence = positive(value, "delivery_sequence", Long.MAX_VALUE);
        long revision = positive(value, "revision", Long.MAX_VALUE);
        long remainingMs = positive(value, "remaining_ms", 10_000L);
        long leaseMs = positive(value, "lease_ms", 800L);
        String status = string(value, "status");
        boolean visible = bool(value, "visible");
        Object advice = value.get("advice");
        if ("candidate_visible".equals(status)) {
            if (!visible || !(advice instanceof Map<?, ?>)) throw new StrictJson.ParseException("candidate_shape");
            @SuppressWarnings("unchecked") Map<String, Object> adviceObject = (Map<String, Object>) advice;
            return new OverlayEnvelope(streamId, sessionId, deliverySequence, revision, renderToken,
                    status, remainingMs, leaseMs, Advice.parse(adviceObject));
        }
        if (!WITHDRAWALS.contains(status) || visible || advice != null) {
            throw new StrictJson.ParseException("withdrawal_shape");
        }
        return new OverlayEnvelope(streamId, sessionId, deliverySequence, revision, renderToken,
                status, remainingMs, leaseMs, null);
    }

    String semanticKey() {
        if (advice == null) return status + "\u0000withdrawn";
        return status + "\u0000" + advice.titleZh + "\u0000" + advice.actionZh + "\u0000" + advice.reasonZh;
    }

    private static long positive(Map<String, Object> value, String key, long maximum) throws StrictJson.ParseException {
        Object raw = value.get(key);
        if (!(raw instanceof Long)) throw new StrictJson.ParseException(key + "_type");
        long number = (Long) raw;
        if (number < 1 || number > maximum) throw new StrictJson.ParseException(key + "_range");
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

    private static void requireKeys(Map<String, Object> value, Set<String> expected) throws StrictJson.ParseException {
        if (!value.keySet().equals(expected)) throw new StrictJson.ParseException("keys");
    }

    static final class Advice {
        final String titleZh;
        final String actionZh;
        final String reasonZh;

        private Advice(String titleZh, String actionZh, String reasonZh) {
            this.titleZh = titleZh;
            this.actionZh = actionZh;
            this.reasonZh = reasonZh;
        }

        static Advice parse(Map<String, Object> value) throws StrictJson.ParseException {
            requireKeys(value, Set.of("title_zh", "action_zh", "reason_zh"));
            return new Advice(text(string(value, "title_zh"), 18), text(string(value, "action_zh"), 28),
                    text(string(value, "reason_zh"), 32));
        }

        private static String text(String value, int maximum) throws StrictJson.ParseException {
            if (value.isBlank() || value.length() > maximum || !value.equals(value.trim())
                    || value.chars().anyMatch(character -> character < 32)) {
                throw new StrictJson.ParseException("text");
            }
            return value;
        }
    }
}
