package com.android.jcc.coachoverlay;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import org.junit.Test;

/** Deterministic local-loopback regressions; they do not start Android Service or contact an external endpoint. */
public final class LoopbackViewPollerTest {
    @Test public void continuouslyWritingStreamCannotOutliveTotalReadDeadline() throws Exception {
        long[] clock = {0L};
        InputStream slow = new InputStream() {
            private int reads;
            @Override public int read() {
                clock[0] += 300L;
                return reads++ < 8 ? 'x' : -1;
            }
        };
        try {
            LoopbackViewPoller.read(slow, 500L, () -> clock[0], () -> false);
            fail("slow stream should hit the total response deadline");
        } catch (IOException expected) {
            // The second chunk advances the controlled monotonic clock past the 500ms total budget.
        }
    }

    @Test public void eofAfterDeadlineCannotProduceBody() throws Exception {
        long[] clock = {499L};
        InputStream lateEof = new ByteArrayInputStream("{}".getBytes(StandardCharsets.UTF_8)) {
            @Override public int read(byte[] value, int offset, int length) {
                int result = super.read(value, offset, length);
                if (result == -1) clock[0] = 501L;
                return result;
            }
        };
        try {
            LoopbackViewPoller.read(lateEof, 500L, () -> clock[0], () -> false);
            fail("EOF after the deadline must not become a candidate");
        } catch (IOException expected) {
            // EOF is rechecked before the body is returned.
        }
    }

    @Test public void stoppedReaderFailsBeforeReadingAnyBodyBytes() throws Exception {
        try {
            LoopbackViewPoller.read(new ByteArrayInputStream("{}".getBytes(StandardCharsets.UTF_8)), 500L, () -> 0L, () -> true);
            fail("stopped reader should not consume a body");
        } catch (InterruptedException expected) {
            // stop() invalidates the generation and actively disconnects the current connection in production.
        }
    }

    @Test public void stopBeforeRegistrationCannotLaterIssueGet() throws Exception {
        ScheduledExecutorService poll = Executors.newSingleThreadScheduledExecutor();
        ScheduledExecutorService deadline = Executors.newSingleThreadScheduledExecutor();
        CountDownLatch beforeRegister = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        Recorder recorder = new Recorder();
        LoopbackViewPoller reader = new LoopbackViewPoller(poll, deadline, recorder, 18181,
                LoopbackViewPollerTest::nowMs, new LoopbackViewPoller.LifecycleHook() {
                    @Override public void beforeRegister() {
                        beforeRegister.countDown();
                        try { release.await(); } catch (InterruptedException ignored) { Thread.currentThread().interrupt(); }
                    }
                });
        try {
            reader.start();
            assertTrue(beforeRegister.await(2, TimeUnit.SECONDS));
            reader.stop();
            release.countDown();
            poll.shutdown();
            assertTrue(poll.awaitTermination(2, TimeUnit.SECONDS));
            assertEquals(0, recorder.envelopes.get());
            assertEquals(0, recorder.disconnects.get());
        } finally {
            release.countDown();
            reader.stop();
            poll.shutdownNow();
            deadline.shutdownNow();
        }
    }

    @Test public void slowResponseHeadersAreDisconnectedByIndependentDeadline() throws Exception {
        assertSlowLoopbackDisconnects(false);
    }

    @Test public void slowResponseBodyIsDisconnectedByIndependentDeadline() throws Exception {
        assertSlowLoopbackDisconnects(true);
    }

    private static void assertSlowLoopbackDisconnects(boolean sendHeaders) throws Exception {
        CountDownLatch requested = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        Recorder recorder = new Recorder();
        ScheduledExecutorService poll = Executors.newSingleThreadScheduledExecutor();
        ScheduledExecutorService deadline = Executors.newSingleThreadScheduledExecutor();
        try (ServerSocket server = loopbackServer()) {
            Thread responder = new Thread(() -> serveSlowResponse(server, requested, release, sendHeaders), "slow-loopback-test");
            responder.start();
            LoopbackViewPoller reader = new LoopbackViewPoller(poll, deadline, recorder, server.getLocalPort(),
                    LoopbackViewPollerTest::nowMs, new LoopbackViewPoller.LifecycleHook() { }, 2_000);
            try {
                reader.start();
                assertTrue(requested.await(2, TimeUnit.SECONDS));
                assertTrue("deadline should force one fail-closed disconnect", recorder.disconnected.await(3, TimeUnit.SECONDS));
                assertEquals(0, recorder.envelopes.get());
            } finally {
                reader.stop();
                release.countDown();
                server.close();
                responder.join(2_000L);
            }
        } finally {
            release.countDown();
            poll.shutdownNow();
            deadline.shutdownNow();
        }
    }

    private static void serveSlowResponse(ServerSocket server, CountDownLatch requested, CountDownLatch release,
                                          boolean sendHeaders) {
        try (Socket socket = server.accept()) {
            readRequest(socket.getInputStream());
            requested.countDown();
            OutputStream output = socket.getOutputStream();
            if (sendHeaders) {
                output.write("HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{".getBytes(StandardCharsets.US_ASCII));
                output.flush();
            }
            release.await();
            if (sendHeaders) output.write('}');
        } catch (Exception ignored) {
            // The client deadline deliberately closes this controlled local socket.
        }
    }

    @Test public void stoppedSlowReaderCannotOccupyNewReaderExecutor() throws Exception {
        CountDownLatch registered = new CountDownLatch(1);
        CountDownLatch releaseOldGet = new CountDownLatch(1);
        CountDownLatch oldRequested = new CountDownLatch(1);
        CountDownLatch oldRelease = new CountDownLatch(1);
        CountDownLatch newRequested = new CountDownLatch(1);
        ScheduledExecutorService oldPoll = Executors.newSingleThreadScheduledExecutor();
        ScheduledExecutorService oldDeadline = Executors.newSingleThreadScheduledExecutor();
        ScheduledExecutorService newPoll = Executors.newSingleThreadScheduledExecutor();
        ScheduledExecutorService newDeadline = Executors.newSingleThreadScheduledExecutor();
        Recorder oldRecorder = new Recorder();
        Recorder newRecorder = new Recorder();
        try (ServerSocket oldServer = loopbackServer(); ServerSocket newServer = loopbackServer()) {
            Thread oldResponder = new Thread(() -> serveSlowResponse(oldServer, oldRequested, oldRelease, false), "old-slow-header");
            Thread newResponder = new Thread(() -> serveFastFailure(newServer, newRequested), "new-fast-response");
            oldResponder.start();
            newResponder.start();
            LoopbackViewPoller oldReader = new LoopbackViewPoller(oldPoll, oldDeadline, oldRecorder, oldServer.getLocalPort(),
                    LoopbackViewPollerTest::nowMs, new LoopbackViewPoller.LifecycleHook() {
                        @Override public void afterRegister() {
                            registered.countDown();
                            try { releaseOldGet.await(); } catch (InterruptedException ignored) { Thread.currentThread().interrupt(); }
                        }
                    }, 2_000);
            LoopbackViewPoller newReader = new LoopbackViewPoller(newPoll, newDeadline, newRecorder, newServer.getLocalPort(),
                    LoopbackViewPollerTest::nowMs, new LoopbackViewPoller.LifecycleHook() { }, 2_000);
            try {
                oldReader.start();
                assertTrue(registered.await(2, TimeUnit.SECONDS));
                oldReader.stop();
                releaseOldGet.countDown();
                assertTrue("controlled old request should enter its slow response", oldRequested.await(1, TimeUnit.SECONDS));
                newReader.start();
                assertTrue("new independent reader must run while old headers are slow", newRequested.await(1, TimeUnit.SECONDS));
                assertEquals(0, oldRecorder.envelopes.get());
            } finally {
                oldReader.stop();
                newReader.stop();
                releaseOldGet.countDown();
                oldRelease.countDown();
                oldServer.close();
                newServer.close();
                oldResponder.join(2_000L);
                newResponder.join(2_000L);
            }
        } finally {
            releaseOldGet.countDown();
            oldRelease.countDown();
            oldPoll.shutdownNow();
            oldDeadline.shutdownNow();
            newPoll.shutdownNow();
            newDeadline.shutdownNow();
        }
    }

    private static ServerSocket loopbackServer() throws IOException {
        return new ServerSocket(0, 50, InetAddress.getByName("127.0.0.1"));
    }

    private static void serveFastFailure(ServerSocket server, CountDownLatch requested) {
        try (Socket socket = server.accept()) {
            readRequest(socket.getInputStream());
            requested.countDown();
            socket.getOutputStream().write("HTTP/1.1 503 Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".getBytes(StandardCharsets.US_ASCII));
        } catch (IOException ignored) {
            // Test cleanup can close this controlled loopback socket.
        }
    }

    private static void readRequest(InputStream input) throws IOException {
        int previous3 = -1, previous2 = -1, previous1 = -1, current;
        while ((current = input.read()) != -1) {
            if (previous3 == '\r' && previous2 == '\n' && previous1 == '\r' && current == '\n') return;
            previous3 = previous2;
            previous2 = previous1;
            previous1 = current;
        }
        throw new IOException("incomplete_request");
    }

    private static long nowMs() { return TimeUnit.NANOSECONDS.toMillis(System.nanoTime()); }

    private static final class Recorder implements LoopbackViewPoller.Listener {
        final AtomicInteger envelopes = new AtomicInteger();
        final AtomicInteger disconnects = new AtomicInteger();
        final CountDownLatch disconnected = new CountDownLatch(1);
        @Override public void onEnvelope(long generation, String envelope) { envelopes.incrementAndGet(); }
        @Override public void onDisconnected(long generation) { disconnects.incrementAndGet(); disconnected.countDown(); }
    }
}
