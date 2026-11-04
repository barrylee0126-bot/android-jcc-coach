package com.android.jcc.coachoverlay;

/** Serial, in-memory synthetic demo session. It has no transport or external input. */
final class SyntheticOverlaySession {
    private OverlayState state = new OverlayState();
    private long generation;
    private long delivery;

    OverlayState.Render show(long nowElapsedMs) {
        generation++;
        delivery++;
        return state.accept(envelope(delivery), nowElapsedMs);
    }

    OverlayState.Render accept(String envelope, long nowElapsedMs) {
        generation++;
        return state.accept(envelope, nowElapsedMs);
    }

    OverlayState.Render clear() {
        generation++;
        return state.disconnect();
    }

    /** A new loopback reader owns a new bridge sequence namespace and must not inherit old watermarks. */
    OverlayState.Render resetForNewObservation() {
        generation++;
        state = new OverlayState();
        return state.clear();
    }

    long generation() { return generation; }

    /** Null means an old callback and must not touch the current card. */
    OverlayState.Render expiry(long scheduledGeneration, long nowElapsedMs) {
        return scheduledGeneration == generation ? state.tick(nowElapsedMs) : null;
    }

    OverlayState.Render current(long nowElapsedMs) { return state.tick(nowElapsedMs); }

    private static String envelope(long delivery) {
        return "{\"schema_version\":\"tablet-overlay-envelope-v1\",\"stream_id\":\"synthetic-stream\","
                + "\"session_id\":\"synthetic-service\",\"delivery_sequence\":" + delivery
                + ",\"revision\":1,\"render_token\":\"synthetic-render-1\","
                + "\"status\":\"candidate_visible\",\"visible\":true,\"remaining_ms\":800,\"lease_ms\":800,"
                + "\"advice\":{\"title_zh\":\"合成开发提示\",\"action_zh\":\"不代表游戏建议\","
                + "\"reason_zh\":\"仅验证平板显示与自动撤回。\"}}";
    }
}
