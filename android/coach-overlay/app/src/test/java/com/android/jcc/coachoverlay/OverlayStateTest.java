package com.android.jcc.coachoverlay;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public final class OverlayStateTest {
    private static String candidate(long delivery, long revision, String token) {
        return candidate("stream", "session", delivery, revision, token, "标题", "动作", "理由");
    }

    private static String candidate(String stream, String session, long delivery, long revision, String token,
                                    String title, String action, String reason) {
        return "{\"schema_version\":\"tablet-overlay-envelope-v1\",\"stream_id\":\"" + stream + "\","
                + "\"session_id\":\"" + session + "\",\"delivery_sequence\":" + delivery
                + ",\"revision\":" + revision + ",\"render_token\":\"" + token
                + "\",\"status\":\"candidate_visible\",\"visible\":true,\"remaining_ms\":700,"
                + "\"lease_ms\":600,\"advice\":{\"title_zh\":\"" + title
                + "\",\"action_zh\":\"" + action + "\",\"reason_zh\":\"" + reason + "\"}}";
    }

    private static String withdrawal(long delivery, long revision) {
        return "{\"schema_version\":\"tablet-overlay-envelope-v1\",\"stream_id\":\"stream\","
                + "\"session_id\":\"session\",\"delivery_sequence\":" + delivery + ",\"revision\":" + revision + ","
                + "\"render_token\":\"render-" + revision + "\",\"status\":\"unknown\",\"visible\":false,"
                + "\"remaining_ms\":700,\"lease_ms\":600,\"advice\":null}";
    }

    @Test public void candidateExpiresUsingSmallerLocalLease() {
        OverlayState state = new OverlayState();
        assertTrue(state.accept(candidate(1, 1, "render-1"), 1000).visible);
        assertTrue(state.tick(1599).visible);
        assertFalse(state.tick(1600).visible);
    }

    @Test public void unknownAndMalformedInputWithdrawPreviousCard() {
        OverlayState state = new OverlayState();
        assertTrue(state.accept(candidate(1, 1, "render-1"), 1000).visible);
        assertFalse(state.accept(withdrawal(2, 2), 1010).visible);
        assertTrue(state.accept(candidate(3, 3, "render-3"), 1020).visible);
        assertFalse(state.accept("{not-json", 1030).visible);
    }

    @Test public void nonadvancingDeliveryOrRevisionConflictWithdraws() {
        OverlayState state = new OverlayState();
        assertTrue(state.accept(candidate(3, 2, "render-2"), 1000).visible);
        assertFalse(state.accept(candidate(3, 2, "render-2"), 1010).visible);
        assertTrue(state.accept(candidate(4, 2, "render-2"), 1020).visible);
        assertFalse(state.accept(candidate(5, 2, "different-token"), 1030).visible);
    }

    @Test public void strictParserRejectsCoercionLaxSyntaxDuplicateAndOverflow() {
        OverlayState state = new OverlayState();
        String raw = candidate(1, 1, "render-1");
        assertFalse(state.accept(raw.replace("\"visible\":true", "\"visible\":\"true\""), 1000).visible);
        assertFalse(state.accept(raw.replace("\"delivery_sequence\":1", "\"delivery_sequence\":9223372036854775808"), 1010).visible);
        assertFalse(state.accept(raw.replace("\"visible\":true,", "\"visible\":true,\"visible\":true,"), 1020).visible);
        assertFalse(state.accept(raw.replace("\"status\":\"candidate_visible\"", "'status'='candidate_visible'"), 1030).visible);
        assertFalse(state.accept(raw.replace("\"delivery_sequence\":1", "\"delivery_sequence\":١"), 1040).visible);
        assertFalse(state.accept(raw.replace("标题", "\\u١٢٣٤"), 1050).visible);
    }

    @Test public void sameRevisionCannotChangeSemanticTextEvenWithNewDelivery() {
        OverlayState state = new OverlayState();
        assertTrue(state.accept(candidate(1, 1, "render-1"), 1000).visible);
        assertTrue(state.accept(candidate(2, 1, "render-1"), 1010).visible);
        assertFalse(state.accept(candidate("stream", "session", 3, 1, "render-1", "标题", "改变动作", "理由"), 1020).visible);
    }

    @Test public void overlongTextFailsClosedRatherThanBeingTruncated() {
        OverlayState state = new OverlayState();
        assertFalse(state.accept(candidate("stream", "session", 1, 1, "render-1",
                "一二三四五六七八九十一二三四五六七八九", "动作", "理由"), 1000).visible);
    }

    @Test public void clearThenShowLeavesNewSyntheticCardVisibleWhenOldExpiryRuns() {
        SyntheticOverlaySession session = new SyntheticOverlaySession();
        assertTrue(session.show(1000).visible);
        long oldGeneration = session.generation();
        assertFalse(session.clear().visible);
        assertTrue(session.show(1010).visible);
        assertTrue(session.expiry(oldGeneration, 1800) == null);
        assertTrue(session.current(1800).visible);
    }

    @Test public void currentSyntheticExpiryWithdrawsAndNewServiceStartsEmpty() {
        SyntheticOverlaySession first = new SyntheticOverlaySession();
        assertTrue(first.show(1000).visible);
        long generation = first.generation();
        assertFalse(first.expiry(generation, 1800).visible);
        SyntheticOverlaySession rebuilt = new SyntheticOverlaySession();
        assertFalse(rebuilt.current(1800).visible);
        assertTrue(rebuilt.show(1810).visible);
    }
}
