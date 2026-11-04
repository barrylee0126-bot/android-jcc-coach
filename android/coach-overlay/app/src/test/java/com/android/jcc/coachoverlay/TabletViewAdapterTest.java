package com.android.jcc.coachoverlay;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

/** Synthetic-only regression cases for the bridge's source and display barriers. */
public final class TabletViewAdapterTest {
    private static String observation(long revision, String token, long sourceSequence, long remaining, long lease) {
        return "{\"schema_version\":\"human-coach-view-v1\",\"stream_id\":\"stream-a\",\"host_clock_domain_id\":\"host-mono\",\"session_id\":\"session-a\",\"revision\":" + revision
                + ",\"render_token\":\"" + token + "\",\"status\":\"candidate_visible\",\"reason\":\"candidate\",\"visible\":true,\"remaining_ms\":" + remaining + ",\"lease_ms\":" + lease
                + ",\"source_kind\":\"replay_candidate\",\"opportunity_id\":\"opportunity-a\",\"source_sequence\":" + sourceSequence + ",\"frame_id\":\"frame-a\",\"context_version\":\"ctx-a\",\"evidence_id\":\"evidence-a\",\"evidence_status\":\"machine_candidate\",\"source_authenticated\":false,\"strategy_verified\":false,\"execution_eligible\":false,\"advice\":null,\"observation\":{\"id\":\"obs-a\",\"title_zh\":\"人口候选\",\"facts_zh\":\"本帧候选4/5\",\"unknown_zh\":\"棋子身份未知\"},\"delivery\":\"loopback_browser_only\",\"tablet_delivery_verified\":false}";
    }

    private static String withdrawal(long revision, String token) {
        return observation(revision, token, 7, 700, 600)
                .replace("\"status\":\"candidate_visible\",\"reason\":\"candidate\",\"visible\":true", "\"status\":\"unknown\",\"reason\":\"source_unknown\",\"visible\":false")
                .replace("\"source_kind\":\"replay_candidate\"", "\"source_kind\":null")
                .replace("\"opportunity_id\":\"opportunity-a\",\"source_sequence\":7,\"frame_id\":\"frame-a\",\"context_version\":\"ctx-a\",\"evidence_id\":\"evidence-a\",\"evidence_status\":\"machine_candidate\"", "\"opportunity_id\":null,\"source_sequence\":null,\"frame_id\":null,\"context_version\":null,\"evidence_id\":null,\"evidence_status\":null")
                .replace("\"advice\":null,\"observation\":{\"id\":\"obs-a\",\"title_zh\":\"人口候选\",\"facts_zh\":\"本帧候选4/5\",\"unknown_zh\":\"棋子身份未知\"}", "\"advice\":null,\"observation\":null");
    }

    @Test public void legalObservationMayHaveUnknownVersionAndEvidence() throws Exception {
        TabletViewAdapter adapter = new TabletViewAdapter();
        String raw = observation(1, "render-a", 7, 700, 600)
                .replace("\"context_version\":\"ctx-a\",\"evidence_id\":\"evidence-a\"", "\"context_version\":null,\"evidence_id\":null");
        String envelope = adapter.adapt(raw, 1000, 40);
        OverlayEnvelope parsed = OverlayEnvelope.parse(envelope);
        assertTrue("observation".equals(parsed.content.kind));
        assertTrue(parsed.expiresAtElapsedMs == 1560L);
        assertTrue(new OverlayState().accept(envelope, 1000).visible);
    }

    @Test public void sourceKindIsVisibleAsAnUntrustedObservationLabel() throws Exception {
        TabletViewAdapter adapter = new TabletViewAdapter();
        OverlayEnvelope replay = OverlayEnvelope.parse(adapter.adapt(observation(1, "render-a", 7, 700, 600), 1000, 20));
        assertEquals("回放机器候选·未认证", replay.content.titleZh);
        assertEquals("人口候选：本帧候选4/5", replay.content.primaryZh);
        TabletViewAdapter syntheticAdapter = new TabletViewAdapter();
        String synthetic = observation(1, "render-a", 7, 700, 600)
                .replace("\"source_kind\":\"replay_candidate\"", "\"source_kind\":\"synthetic\"");
        assertEquals("合成演示·未认证", OverlayEnvelope.parse(syntheticAdapter.adapt(synthetic, 1000, 20)).content.titleZh);
        TabletViewAdapter liveAdapter = new TabletViewAdapter();
        String live = observation(1, "render-a", 7, 700, 600)
                .replace("\"source_kind\":\"replay_candidate\"", "\"source_kind\":\"live_candidate\"");
        assertEquals("实时机器候选·未认证", OverlayEnvelope.parse(liveAdapter.adapt(live, 1000, 20)).content.titleZh);
        TabletViewAdapter replayAdapter = new TabletViewAdapter();
        String replayOnly = observation(1, "render-a", 7, 700, 600)
                .replace("\"source_kind\":\"replay_candidate\"", "\"source_kind\":\"replay\"");
        assertEquals("历史回放·未认证", OverlayEnvelope.parse(replayAdapter.adapt(replayOnly, 1000, 20)).content.titleZh);
    }

    @Test public void sourceAndObservationThatCannotShareAFullLineAreWithdrawn() throws Exception {
        String tooLong = observation(1, "render-a", 7, 700, 600)
                .replace("\"facts_zh\":\"本帧候选4/5\"", "\"facts_zh\":\"一二三四五六七八九十一二三四五六七八九十一二三四五六七\"");
        assertEquals("unknown", OverlayEnvelope.parse(new TabletViewAdapter().adapt(tooLong, 1000, 20)).status);
    }

    @Test public void realHumanCoachSyntheticStartupUnknownThenFirstCandidateReachesOverlay() {
        // Generated from HumanCoachStore with a fixed UUID, then augmented only by 25E's two delivery fields.
        String startup = "{\"schema_version\":\"human-coach-view-v1\",\"stream_id\":\"synthetic-stream\",\"host_clock_domain_id\":\"host-mono-12345678123456781234567812345678\",\"session_id\":\"coach-session-12345678123456781234567812345678\",\"revision\":0,\"render_token\":\"render-0\",\"status\":\"unknown\",\"reason\":\"waiting_for_candidate\",\"visible\":false,\"remaining_ms\":0,\"lease_ms\":0,\"source_kind\":null,\"opportunity_id\":null,\"source_sequence\":null,\"frame_id\":null,\"context_version\":null,\"evidence_id\":null,\"evidence_status\":null,\"source_authenticated\":false,\"strategy_verified\":false,\"execution_eligible\":false,\"advice\":null,\"observation\":null,\"delivery\":\"loopback_browser_only\",\"tablet_delivery_verified\":false}";
        String candidate = "{\"schema_version\":\"human-coach-view-v1\",\"stream_id\":\"synthetic-stream\",\"host_clock_domain_id\":\"host-mono-12345678123456781234567812345678\",\"session_id\":\"coach-session-12345678123456781234567812345678\",\"revision\":1,\"render_token\":\"render-1\",\"status\":\"candidate_visible\",\"reason\":\"candidate\",\"visible\":true,\"remaining_ms\":800,\"lease_ms\":800,\"source_kind\":\"synthetic\",\"opportunity_id\":\"synthetic-stream:1\",\"source_sequence\":1,\"frame_id\":\"synthetic-frame\",\"context_version\":null,\"evidence_id\":null,\"evidence_status\":\"machine_candidate\",\"source_authenticated\":false,\"strategy_verified\":false,\"execution_eligible\":false,\"advice\":null,\"observation\":{\"id\":\"synthetic-observation\",\"title_zh\":\"人口候选\",\"facts_zh\":\"本帧候选4/5\",\"unknown_zh\":\"棋子身份未知\"},\"delivery\":\"loopback_browser_only\",\"tablet_delivery_verified\":false}";
        TabletViewAdapter adapter = new TabletViewAdapter();
        OverlayState overlay = new OverlayState();
        String startupEnvelope = adapter.adapt(startup, 1000, 10);
        assertTrue(startupEnvelope.contains("\"source_revision\":0"));
        assertFalse(overlay.accept(startupEnvelope, 1000).visible);
        assertTrue(overlay.accept(adapter.adapt(candidate, 1010, 10), 1010).visible);
    }

    @Test public void newSessionHasItsOwnSourceSequenceNamespace() {
        TabletViewAdapter adapter = new TabletViewAdapter();
        OverlayState overlay = new OverlayState();
        assertTrue(overlay.accept(adapter.adapt(observation(1, "render-a", 100, 700, 600), 1000, 20), 1000).visible);
        String newUnknown = withdrawal(0, "render-0").replace("\"session_id\":\"session-a\"", "\"session_id\":\"session-b\"");
        assertFalse(overlay.accept(adapter.adapt(newUnknown, 1010, 20), 1010).visible);
        String firstNew = observation(1, "render-1", 1, 700, 600).replace("\"session_id\":\"session-a\"", "\"session_id\":\"session-b\"");
        assertTrue(overlay.accept(adapter.adapt(firstNew, 1020, 20), 1020).visible);
    }

    @Test public void retiredSessionCannotPolluteCurrentSessionOrRenewItsCard() throws Exception {
        TabletViewAdapter adapter = new TabletViewAdapter();
        OverlayState overlay = new OverlayState();
        String firstA = observation(1, "render-a", 1, 700, 600).replace("\"session_id\":\"session-a\"", "\"session_id\":\"session-a1\"");
        String startupB = withdrawal(0, "render-0").replace("\"session_id\":\"session-a\"", "\"session_id\":\"session-b1\"");
        String firstB = observation(1, "render-1", 1, 700, 600).replace("\"session_id\":\"session-a\"", "\"session_id\":\"session-b1\"");
        assertTrue(overlay.accept(adapter.adapt(firstA, 1000, 20), 1000).visible);
        assertFalse(overlay.accept(adapter.adapt(startupB, 1010, 20), 1010).visible);
        assertTrue(overlay.accept(adapter.adapt(firstB, 1020, 20), 1020).visible);
        String lateA = adapter.adapt(firstA, 1030, 20);
        assertTrue("conflict".equals(OverlayEnvelope.parse(lateA).status));
        assertFalse(overlay.accept(lateA, 1030).visible);
        assertNull(adapter.adapt(firstB, 1040, 20));
    }

    @Test public void locallyGeneratedErrorDoesNotPretendToHaveAnUpstreamRevision() {
        String envelope = new TabletViewAdapter().adapt("{bad-json", 1000, 20);
        assertTrue(envelope.contains("\"source_revision\":null"));
    }

    @Test public void malformedViewWithdrawsButDoesNotTerminallyStopTheReader() throws Exception {
        TabletViewAdapter adapter = new TabletViewAdapter();
        assertEquals("unknown", OverlayEnvelope.parse(adapter.adapt("{bad-json", 1000, 20)).status);
        assertFalse(adapter.terminal());
        assertNotNull(adapter.adapt(observation(1, "render-a", 7, 700, 600), 1010, 20));
    }

    @Test public void sessionCapacityFailsClosedForCandidateAndUnknownPaths() throws Exception {
        assertCapacityClosure(false);
        assertCapacityClosure(true);
    }

    @Test public void terminalPollerStopDoesNotFilterItsClearButOldDisconnectCannotClearNewConnection() {
        ConnectionGeneration pollerFence = new ConnectionGeneration();
        ConnectionGeneration serviceFence = new ConnectionGeneration();
        long oldPoller = pollerFence.open();
        long oldService = serviceFence.open();
        // Poller stop invalidates only its own fence; its already-posted clear is still valid to the service.
        pollerFence.invalidate();
        assertFalse(pollerFence.current(oldPoller));
        assertTrue(serviceFence.current(oldService));
        // A subsequent explicit connection makes the old disconnect callback harmless.
        long newService = serviceFence.open();
        assertFalse(serviceFence.current(oldService));
        assertTrue(serviceFence.current(newService));
    }

    private static void assertCapacityClosure(boolean unknownPath) throws Exception {
        TabletViewAdapter adapter = new TabletViewAdapter();
        for (int index = 0; index <= 8; index++) {
            String session = "session-cap-" + index;
            String raw = unknownPath && index > 0
                    ? withdrawal(0, "render-0")
                    : observation(1, "render-1", 1, 700, 600);
            raw = raw.replace("\"session_id\":\"session-a\"", "\"session_id\":\"" + session + "\"");
            assertNotNull(adapter.adapt(raw, 1000 + index, 20));
            assertFalse(adapter.terminal());
        }
        String overflow = unknownPath ? withdrawal(0, "render-0") : observation(1, "render-1", 1, 700, 600);
        overflow = overflow.replace("\"session_id\":\"session-a\"", "\"session_id\":\"session-cap-9\"");
        assertTrue("conflict".equals(OverlayEnvelope.parse(adapter.adapt(overflow, 1010, 20)).status));
        assertTrue(adapter.terminal());
        assertNull(adapter.adapt(overflow, 1020, 20));
        // A new poller owns a new local connection. That reset is not authentication.
        assertNotNull(new TabletViewAdapter().adapt(overflow, 1030, 20));
    }

    @Test public void expiredBeforeMainThreadDisplayCannotBecomeVisible() {
        TabletViewAdapter adapter = new TabletViewAdapter();
        String envelope = adapter.adapt(observation(1, "render-a", 7, 700, 600), 1000, 40);
        assertFalse(new OverlayState().accept(envelope, 1560).visible);
    }

    @Test public void oldWithdrawalCannotLowerFenceAndReviveCandidate() throws Exception {
        TabletViewAdapter adapter = new TabletViewAdapter();
        assertNotNull(adapter.adapt(observation(1, "render-a", 7, 700, 600), 1000, 20));
        assertTrue("conflict".equals(OverlayEnvelope.parse(adapter.adapt(withdrawal(0, "render-zero"), 1010, 20)).status));
        assertNull(adapter.adapt(observation(1, "render-a", 7, 700, 600), 1020, 20));
    }

    @Test public void sameFrameSequenceCannotChangeBoundIdentity() throws Exception {
        TabletViewAdapter adapter = new TabletViewAdapter();
        assertNotNull(adapter.adapt(observation(1, "render-a", 7, 700, 600), 1000, 20));
        String changedFrame = observation(1, "render-a", 7, 700, 600).replace("\"frame_id\":\"frame-a\"", "\"frame_id\":\"frame-b\"");
        assertTrue("conflict".equals(OverlayEnvelope.parse(adapter.adapt(changedFrame, 1020, 20)).status));
    }

    @Test public void trustClaimsUnknownSourceAndAdviceAreRejected() throws Exception {
        TabletViewAdapter adapter = new TabletViewAdapter();
        String unknownSource = observation(1, "render-a", 7, 700, 600).replace("\"source_kind\":\"replay_candidate\"", "\"source_kind\":\"anything\"");
        assertTrue("unknown".equals(OverlayEnvelope.parse(adapter.adapt(unknownSource, 1000, 20)).status));
        String falseAuth = observation(1, "render-a", 7, 700, 600).replace("\"source_authenticated\":false", "\"source_authenticated\":true");
        assertTrue("unknown".equals(OverlayEnvelope.parse(adapter.adapt(falseAuth, 1010, 20)).status));
        String advice = observation(1, "render-a", 7, 700, 600).replace("\"advice\":null", "\"advice\":{\"id\":\"a\",\"category\":\"lineup\",\"title_zh\":\"x\",\"action_zh\":\"y\",\"reason_zh\":\"z\"}").replace("\"observation\":{\"id\":\"obs-a\",\"title_zh\":\"人口候选\",\"facts_zh\":\"本帧候选4/5\",\"unknown_zh\":\"棋子身份未知\"}", "\"observation\":null");
        assertTrue("unknown".equals(OverlayEnvelope.parse(adapter.adapt(advice, 1020, 20)).status));
    }

    @Test public void generationFenceRejectsLateCallbacksAfterStopOrRestart() {
        ConnectionGeneration fence = new ConnectionGeneration();
        long first = fence.open();
        assertTrue(fence.current(first));
        fence.invalidate();
        assertFalse(fence.current(first));
        long second = fence.open();
        assertTrue(fence.current(second));
        assertFalse(fence.current(first));
    }

    @Test public void syntheticModeSwitchMakesQueuedLoopbackCallbackHarmless() {
        ConnectionGeneration loopback = new ConnectionGeneration();
        SyntheticOverlaySession local = new SyntheticOverlaySession();
        long observationGeneration = loopback.open();
        // The Service's SHOW_SYNTHETIC path first invalidates the observation generation.
        loopback.invalidate();
        assertTrue(local.show(1000).visible);
        assertFalse(loopback.current(observationGeneration));
        // A previously queued network callback has no current generation with which to render.
        assertTrue(local.current(1010).visible);
    }

    @Test public void reconnectSameUpstreamFrameStartsWithFreshBridgeAndDisplayState() {
        String source = observation(1, "render-a", 1, 700, 600);
        SyntheticOverlaySession display = new SyntheticOverlaySession();
        String first = new TabletViewAdapter().adapt(source, 1000, 20);
        assertTrue(display.accept(first, 1000).visible);
        long oldGeneration = display.generation();
        assertFalse(display.clear().visible);
        assertFalse(display.resetForNewObservation().visible);
        // A newly constructed poller/adapter restarts its bridge delivery sequence at one.
        String reconnect = new TabletViewAdapter().adapt(source, 1020, 20);
        assertTrue(display.accept(reconnect, 1020).visible);
        assertNull(display.expiry(oldGeneration, 1700));
        assertTrue(display.current(1030).visible);
    }
}
