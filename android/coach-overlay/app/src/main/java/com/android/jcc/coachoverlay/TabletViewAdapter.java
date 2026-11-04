package com.android.jcc.coachoverlay;

import java.util.Map;
import java.util.LinkedHashSet;
import java.util.Set;
import java.util.regex.Pattern;

/** Strict, stateful 25E view adapter. It never upgrades an observation to advice. */
final class TabletViewAdapter {
    private static final String SOURCE_SCHEMA = "human-coach-view-v1";
    private static final String OUTPUT_SCHEMA = "tablet-overlay-envelope-v3";
    private static final Pattern ID = Pattern.compile("[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}");
    private static final Set<String> WITHDRAWALS = Set.of("unknown", "stale", "conflict", "scene_changed", "disconnected", "expired");
    private static final Set<String> SOURCE_KINDS = Set.of("synthetic", "replay", "replay_candidate", "live_candidate");
    private static final Set<String> EVIDENCE = Set.of("unverified", "machine_candidate", "independently_reviewed");
    private static final int MAX_RETIRED_SESSIONS = 8;
    private static final Set<String> VIEW_KEYS = Set.of("schema_version", "stream_id", "host_clock_domain_id", "session_id", "revision", "render_token", "status", "reason", "visible", "remaining_ms", "lease_ms", "source_kind", "opportunity_id", "source_sequence", "frame_id", "context_version", "evidence_id", "evidence_status", "source_authenticated", "strategy_verified", "execution_eligible", "advice", "observation", "delivery", "tablet_delivery_verified");

    private String streamId, sessionId, renderToken, lastViewKey;
    private long revision = -1L, sourceSequence = -1L, deliverySequence, bridgeRevision;
    private boolean lastWasWithdrawal;
    private boolean terminal;
    private final LinkedHashSet<String> retiredSessions = new LinkedHashSet<>();

    /** completedElapsedMs is Android elapsedRealtime captured immediately after the response body is read. */
    String adapt(String rawView, long completedElapsedMs, long roundTripMs) {
        if (terminal) return null;
        if (completedElapsedMs < 1 || roundTripMs < 0 || roundTripMs > 5_000L) return withdrawal("unknown");
        try {
            Map<String, Object> view = StrictJson.object(rawView);
            requireKeys(view, VIEW_KEYS);
            if (!SOURCE_SCHEMA.equals(string(view, "schema_version")) || !"loopback_browser_only".equals(string(view, "delivery"))
                    || bool(view, "tablet_delivery_verified") || bool(view, "source_authenticated")
                    || bool(view, "strategy_verified") || bool(view, "execution_eligible")) throw new StrictJson.ParseException("trust_claim");
            String stream = identifier(string(view, "stream_id"));
            String hostClock = identifier(string(view, "host_clock_domain_id"));
            String session = identifier(string(view, "session_id"));
            long incomingRevision = nonnegative(view, "revision", Long.MAX_VALUE);
            String token = identifier(string(view, "render_token"));
            boolean visible = bool(view, "visible");
            String status = string(view, "status");
            boolean sameSession = stream.equals(streamId) && session.equals(sessionId);
            if (!sameSession && retiredSessions.contains(identity(stream, session))) return withdrawal("conflict");
            if (!sameSession && streamId != null && retiredSessions.size() >= MAX_RETIRED_SESSIONS) {
                terminal = true;
                return withdrawal("conflict");
            }
            if (!visible) return acceptWithdrawal(stream, session, incomingRevision, token, status, sameSession);
            if (!"candidate_visible".equals(status) || !"candidate".equals(string(view, "reason")) || incomingRevision < 1) throw new StrictJson.ParseException("candidate_status");
            long remaining = positive(view, "remaining_ms", 10_000L) - roundTripMs;
            long lease = positive(view, "lease_ms", 800L) - roundTripMs;
            String sourceKind = enumValue(view, "source_kind", SOURCE_KINDS);
            String opportunity = identifier(string(view, "opportunity_id"));
            long sequence = positive(view, "source_sequence", Long.MAX_VALUE);
            String frame = identifier(string(view, "frame_id"));
            String context = nullableIdentifier(view, "context_version");
            String evidence = nullableIdentifier(view, "evidence_id");
            String evidenceStatus = enumValue(view, "evidence_status", EVIDENCE);
            Object rawAdvice = view.get("advice"), rawObservation = view.get("observation");
            if (rawAdvice != null || rawObservation == null) throw new StrictJson.ParseException("only_observation_supported");
            Map<String, Object> observation = object(rawObservation, "observation");
            requireKeys(observation, Set.of("id", "title_zh", "facts_zh", "unknown_zh"));
            String observationId = identifier(string(observation, "id"));
            String title = text(string(observation, "title_zh"), 12);
            String facts = text(string(observation, "facts_zh"), 28);
            String unknown = text(string(observation, "unknown_zh"), 29);
            String primary = title + "：" + facts;
            // The renderer has exactly three full lines. Reject, rather than truncate, a source/object pair
            // that cannot both be shown without hiding meaning.
            if (primary.length() > 28) throw new StrictJson.ParseException("observation_display_width");
            String key = join(stream, hostClock, session, Long.toString(incomingRevision), token, sourceKind, opportunity, Long.toString(sequence), frame,
                    nullable(context), nullable(evidence), evidenceStatus, observationId, title, facts, unknown);
            if (sameSession) {
                if (incomingRevision < revision || (incomingRevision == revision && (!token.equals(renderToken)
                        || sequence != sourceSequence || !key.equals(lastViewKey)))) return withdrawal("conflict");
                if (incomingRevision > revision && sequence <= sourceSequence) return withdrawal("conflict");
            }
            if (remaining < 1 || lease < 1) {
                remember(stream, session, incomingRevision, token, sequence, key, false);
                return withdrawal("expired");
            }
            long duration = Math.min(remaining, lease);
            if (completedElapsedMs > Long.MAX_VALUE - duration) return withdrawal("unknown");
            if (key.equals(lastViewKey) && !lastWasWithdrawal) return null;
            remember(stream, session, incomingRevision, token, sequence, key, false);
            return candidate(stream, session, incomingRevision, token, remaining, lease, completedElapsedMs + duration,
                    sourceKind, sequence, frame, context, evidence, evidenceStatus, title, primary, unknown);
        } catch (Exception ignored) {
            return withdrawal("unknown");
        }
    }

    String disconnected() { return withdrawal("disconnected"); }

    boolean terminal() { return terminal; }

    private String acceptWithdrawal(String stream, String session, long incomingRevision, String token, String status,
                                    boolean sameSession) throws StrictJson.ParseException {
        if (!WITHDRAWALS.contains(status)) throw new StrictJson.ParseException("withdrawal_status");
        String key = join(stream, session, Long.toString(incomingRevision), token, status);
        if (sameSession && (incomingRevision < revision || (incomingRevision == revision && (!token.equals(renderToken) || !lastWasWithdrawal)))) {
            return withdrawal("conflict");
        }
        if (sameSession && incomingRevision == revision && key.equals(lastViewKey)) return null;
        remember(stream, session, incomingRevision, token, sameSession ? sourceSequence : -1L, key, true);
        return upstreamWithdrawal(status, incomingRevision);
    }

    private void remember(String stream, String session, long incomingRevision, String token, long sequence, String key,
                          boolean withdrawal) {
        if (streamId != null && !identity(stream, session).equals(identity(streamId, sessionId))) {
            retire(identity(streamId, sessionId));
            sourceSequence = -1L;
        }
        streamId = stream; sessionId = session; revision = incomingRevision; renderToken = token;
        sourceSequence = sequence; lastViewKey = key; lastWasWithdrawal = withdrawal;
    }

    private String candidate(String stream, String session, long sourceRevision, String token, long remaining, long lease,
                             long deadline, String sourceKind, long sequence, String frame, String context, String evidence,
                             String evidenceStatus, String title, String facts, String unknown) {
        return "{\"schema_version\":\"" + OUTPUT_SCHEMA + "\",\"stream_id\":" + quote(stream) + ",\"session_id\":" + quote(session)
                + ",\"delivery_sequence\":" + (++deliverySequence) + ",\"bridge_revision\":" + (++bridgeRevision) + ",\"source_revision\":" + sourceRevision + ",\"render_token\":" + quote(token)
                + ",\"status\":\"candidate_visible\",\"visible\":true,\"remaining_ms\":" + remaining + ",\"lease_ms\":" + lease
                + ",\"expires_at_elapsed_ms\":" + deadline + ",\"source_kind\":" + quote(sourceKind) + ",\"source_sequence\":" + sequence
                + ",\"frame_id\":" + quote(frame) + ",\"context_version\":" + quoteNullable(context) + ",\"evidence_id\":" + quoteNullable(evidence)
                + ",\"evidence_status\":" + quote(evidenceStatus) + ",\"content_kind\":\"observation\",\"content\":{\"title_zh\":"
                + quote(label(sourceKind)) + ",\"primary_zh\":" + quote(facts) + ",\"detail_zh\":" + quote("未知：" + unknown) + "}}";
    }

    private String withdrawal(String status) {
        String stream = streamId == null ? "bridge-stream" : streamId;
        String session = sessionId == null ? "bridge-session" : sessionId;
        String token = renderToken == null ? "bridge-withdrawal" : renderToken;
        return "{\"schema_version\":\"" + OUTPUT_SCHEMA + "\",\"stream_id\":" + quote(stream) + ",\"session_id\":" + quote(session)
                + ",\"delivery_sequence\":" + (++deliverySequence) + ",\"bridge_revision\":" + (++bridgeRevision) + ",\"source_revision\":null,\"render_token\":" + quote(token)
                + ",\"status\":" + quote(status) + ",\"visible\":false,\"remaining_ms\":1,\"lease_ms\":1,\"expires_at_elapsed_ms\":null"
                + ",\"source_kind\":null,\"source_sequence\":null,\"frame_id\":null,\"context_version\":null,\"evidence_id\":null,\"evidence_status\":null,\"content_kind\":\"none\",\"content\":null}";
    }

    private String upstreamWithdrawal(String status, long sourceRevision) {
        String stream = streamId == null ? "bridge-stream" : streamId;
        String session = sessionId == null ? "bridge-session" : sessionId;
        String token = renderToken == null ? "bridge-withdrawal" : renderToken;
        return "{\"schema_version\":\"" + OUTPUT_SCHEMA + "\",\"stream_id\":" + quote(stream) + ",\"session_id\":" + quote(session)
                + ",\"delivery_sequence\":" + (++deliverySequence) + ",\"bridge_revision\":" + (++bridgeRevision) + ",\"source_revision\":" + sourceRevision + ",\"render_token\":" + quote(token)
                + ",\"status\":" + quote(status) + ",\"visible\":false,\"remaining_ms\":1,\"lease_ms\":1,\"expires_at_elapsed_ms\":null"
                + ",\"source_kind\":null,\"source_sequence\":null,\"frame_id\":null,\"context_version\":null,\"evidence_id\":null,\"evidence_status\":null,\"content_kind\":\"none\",\"content\":null}";
    }

    private void retire(String value) {
        retiredSessions.remove(value);
        retiredSessions.add(value);
    }

    private static String label(String sourceKind) {
        if ("synthetic".equals(sourceKind)) return "合成演示·未认证";
        if ("replay".equals(sourceKind)) return "历史回放·未认证";
        if ("replay_candidate".equals(sourceKind)) return "回放机器候选·未认证";
        return "实时机器候选·未认证";
    }

    private static Map<String, Object> object(Object value, String reason) throws StrictJson.ParseException { if (!(value instanceof Map<?, ?>)) throw new StrictJson.ParseException(reason); @SuppressWarnings("unchecked") Map<String, Object> result = (Map<String, Object>) value; return result; }
    private static String string(Map<String, Object> value, String key) throws StrictJson.ParseException { Object raw = value.get(key); if (!(raw instanceof String)) throw new StrictJson.ParseException(key); return (String) raw; }
    private static boolean bool(Map<String, Object> value, String key) throws StrictJson.ParseException { Object raw = value.get(key); if (!(raw instanceof Boolean)) throw new StrictJson.ParseException(key); return (Boolean) raw; }
    private static long positive(Map<String, Object> value, String key, long maximum) throws StrictJson.ParseException { long number = nonnegative(value, key, maximum); if (number < 1) throw new StrictJson.ParseException(key); return number; }
    private static long nonnegative(Map<String, Object> value, String key, long maximum) throws StrictJson.ParseException { Object raw = value.get(key); if (!(raw instanceof Long) || (Long) raw < 0 || (Long) raw > maximum) throw new StrictJson.ParseException(key); return (Long) raw; }
    private static String identifier(String value) throws StrictJson.ParseException { if (!ID.matcher(value).matches()) throw new StrictJson.ParseException("identifier"); return value; }
    private static String nullableIdentifier(Map<String, Object> value, String key) throws StrictJson.ParseException { Object raw = value.get(key); if (raw == null) return null; if (!(raw instanceof String)) throw new StrictJson.ParseException(key); return identifier((String) raw); }
    private static String enumValue(Map<String, Object> value, String key, Set<String> allowed) throws StrictJson.ParseException { String result = string(value, key); if (!allowed.contains(result)) throw new StrictJson.ParseException(key); return result; }
    private static String text(String value, int max) throws StrictJson.ParseException { if (value.isBlank() || value.length() > max || !value.equals(value.trim()) || value.chars().anyMatch(c -> c < 32)) throw new StrictJson.ParseException("text"); return value; }
    private static void requireKeys(Map<String, Object> value, Set<String> expected) throws StrictJson.ParseException { if (!value.keySet().equals(expected)) throw new StrictJson.ParseException("keys"); }
    private static String nullable(String value) { return value == null ? "<null>" : value; }
    private static String identity(String stream, String session) { return stream + "\u0000" + session; }
    private static String join(String... values) { return String.join("\u0000", values); }
    private static String quoteNullable(String value) { return value == null ? "null" : quote(value); }
    private static String quote(String value) { StringBuilder output = new StringBuilder(value.length() + 2).append('"'); for (int i = 0; i < value.length(); i++) { char c = value.charAt(i); switch (c) { case '"': output.append("\\\""); break; case '\\': output.append("\\\\"); break; case '\b': output.append("\\b"); break; case '\f': output.append("\\f"); break; case '\n': output.append("\\n"); break; case '\r': output.append("\\r"); break; case '\t': output.append("\\t"); break; default: if (c < 32) output.append(String.format("\\u%04x", (int) c)); else output.append(c); } } return output.append('"').toString(); }
}
