package com.android.jcc.coachoverlay;

/** Keeps one local display lease and fails closed on stale or malformed input. */
final class OverlayState {
    private String streamId;
    private String sessionId;
    private long deliverySequence;
    private long revision;
    private String renderToken;
    private String semanticKey;
    private Render current = Render.hidden();

    OverlayState() { }

    Render accept(String raw, long receivedElapsedMs) {
        final OverlayEnvelope incoming;
        try {
            incoming = OverlayEnvelope.parse(raw);
        } catch (Exception ignored) {
            return clear();
        }
        boolean sameIdentity = incoming.streamId.equals(streamId) && incoming.sessionId.equals(sessionId);
        if (sameIdentity && (incoming.deliverySequence <= deliverySequence || incoming.revision < revision
                || (incoming.revision == revision && (!incoming.renderToken.equals(renderToken)
                || !incoming.semanticKey().equals(semanticKey))))) {
            return clear();
        }
        if (!sameIdentity) {
            clear();
        }
        streamId = incoming.streamId;
        sessionId = incoming.sessionId;
        deliverySequence = incoming.deliverySequence;
        revision = incoming.revision;
        renderToken = incoming.renderToken;
        semanticKey = incoming.semanticKey();
        if (incoming.content == null) return clear();
        long duration = Math.min(incoming.remainingMs, incoming.leaseMs);
        long expiresAt = incoming.expiresAtElapsedMs > 0 ? incoming.expiresAtElapsedMs : receivedElapsedMs + duration;
        if (expiresAt <= receivedElapsedMs) return clear();
        current = new Render(incoming.content.titleZh, incoming.content.primaryZh, incoming.content.detailZh, expiresAt);
        return current;
    }

    Render tick(long nowElapsedMs) {
        return current.visible && nowElapsedMs >= current.expiresAtElapsedMs ? clear() : current;
    }

    Render disconnect() {
        return clear();
    }

    Render clear() {
        current = Render.hidden();
        return current;
    }

    static final class Render {
        final boolean visible;
        final String titleZh;
        final String actionZh;
        final String reasonZh;
        final long expiresAtElapsedMs;

        Render(String titleZh, String actionZh, String reasonZh, long expiresAtElapsedMs) {
            this.visible = true;
            this.titleZh = titleZh;
            this.actionZh = actionZh;
            this.reasonZh = reasonZh;
            this.expiresAtElapsedMs = expiresAtElapsedMs;
        }

        static Render hidden() { return new Render(); }

        private Render() {
            this.visible = false;
            this.titleZh = "";
            this.actionZh = "";
            this.reasonZh = "";
            this.expiresAtElapsedMs = 0L;
        }
    }
}
