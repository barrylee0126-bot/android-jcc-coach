package com.android.jcc.coachoverlay;

import android.os.SystemClock;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;
import java.util.function.BooleanSupplier;

/** 127.0.0.1 reader for an adb-reverse mapped endpoint; it never listens on a port. */
final class LoopbackViewPoller {
    private static final long PERIOD_MS = 125L;
    private static final int TIMEOUT_MS = 250;
    /** Deadline task disconnects slow headers/bodies; Android socket interruption timing still needs device verification. */
    private static final long TOTAL_RESPONSE_MS = 500L;
    private static final int MAX_BYTES = 4096;

    interface Listener {
        void onEnvelope(long generation, String envelope);
        void onDisconnected(long generation);
    }

    interface MonotonicClock { long now(); }

    /** Test seam only: controlled pauses around registration; production uses the no-op hook. */
    interface LifecycleHook {
        default void beforeRegister() { }
        default void afterRegister() { }
    }

    private static final LifecycleHook NO_HOOK = new LifecycleHook() { };
    private final Object lifecycleLock = new Object();
    private final ScheduledExecutorService executor;
    private final ScheduledExecutorService deadlineExecutor;
    private final Listener listener;
    private final TabletViewAdapter adapter = new TabletViewAdapter();
    private final ConnectionGeneration fence = new ConnectionGeneration();
    private final int port;
    private final int requestTimeoutMs;
    private final MonotonicClock clock;
    private final LifecycleHook hook;
    private ScheduledFuture<?> task;
    private ScheduledFuture<?> deadlineTask;
    /** Access only while lifecycleLock is held. */
    private HttpURLConnection activeConnection;
    /** Access only while lifecycleLock is held. */
    private boolean stopped = true;

    LoopbackViewPoller(ScheduledExecutorService executor, ScheduledExecutorService deadlineExecutor,
                       Listener listener, int port) {
        this(executor, deadlineExecutor, listener, port, SystemClock::elapsedRealtime, NO_HOOK);
    }

    LoopbackViewPoller(ScheduledExecutorService executor, ScheduledExecutorService deadlineExecutor,
                       Listener listener, int port, MonotonicClock clock, LifecycleHook hook) {
        this(executor, deadlineExecutor, listener, port, clock, hook, TIMEOUT_MS);
    }

    LoopbackViewPoller(ScheduledExecutorService executor, ScheduledExecutorService deadlineExecutor,
                       Listener listener, int port, MonotonicClock clock, LifecycleHook hook, int requestTimeoutMs) {
        this.executor = executor;
        this.deadlineExecutor = deadlineExecutor;
        this.listener = listener;
        this.port = port;
        this.clock = clock;
        this.hook = hook;
        this.requestTimeoutMs = requestTimeoutMs;
    }

    void start() {
        synchronized (lifecycleLock) {
            if (task != null) return;
            stopped = false;
            long generation = fence.open();
            task = executor.scheduleWithFixedDelay(() -> poll(generation), 0L, PERIOD_MS, TimeUnit.MILLISECONDS);
        }
    }

    /**
     * Non-blocking stop: invalidates callbacks immediately but never calls network code on the caller thread.
     * The Service schedules closeConnection() on a per-reader background executor and keeps the deadline watchdog.
     */
    void stop() { stopRetiredGeneration(); }

    /** May block inside the platform HTTP stack; callers must use a background executor. */
    void closeConnection() {
        HttpURLConnection connection;
        synchronized (lifecycleLock) { connection = activeConnection; }
        if (connection != null) connection.disconnect();
    }

    private void poll(long generation) {
        if (!active(generation)) return;
        HttpURLConnection connection = null;
        try {
            long started = clock.now();
            long deadline = deadlineAfter(started);
            connection = (HttpURLConnection) new URL("http://127.0.0.1:" + port + "/api/view").openConnection();
            final HttpURLConnection current = connection;
            current.setRequestMethod("GET");
            current.setConnectTimeout(requestTimeoutMs);
            current.setReadTimeout(requestTimeoutMs);
            current.setUseCaches(false);
            current.setRequestProperty("Cache-Control", "no-cache");
            current.setRequestProperty("Accept", "application/json");
            hook.beforeRegister();
            if (!registerConnection(generation, current, deadline)) {
                if (endGeneration(generation, true)) listener.onDisconnected(generation);
                return;
            }
            hook.afterRegister();
            // getResponseCode may block waiting for headers. The independent deadline task disconnects it.
            if (current.getResponseCode() != HttpURLConnection.HTTP_OK) throw new IllegalStateException("http_status");
            requireCurrent(generation, current, deadline);
            String raw = read(current.getInputStream(), deadline, clock,
                    () -> !currentConnection(generation, current) || Thread.currentThread().isInterrupted());
            // Recheck after EOF and again immediately before producing a display candidate.
            requireCurrent(generation, current, deadline);
            long completed = clock.now();
            String envelope = adapter.adapt(raw, completed, completed - started);
            requireCurrent(generation, current, deadline);
            if (envelope != null) listener.onEnvelope(generation, envelope);
            if (adapter.terminal() && endGeneration(generation, true)) {
                // Capacity is a fail-closed connection boundary, not a silent cache eviction.
                listener.onDisconnected(generation);
            }
        } catch (Exception ignored) {
            if (endGeneration(generation, true)) listener.onDisconnected(generation);
        } finally {
            clearConnection(connection);
            if (connection != null) connection.disconnect();
        }
    }

    private static long deadlineAfter(long started) {
        return started > Long.MAX_VALUE - TOTAL_RESPONSE_MS ? Long.MAX_VALUE : started + TOTAL_RESPONSE_MS;
    }

    /** Registration and stop share this lock: stop cannot observe no connection then allow a later GET. */
    private boolean registerConnection(long generation, HttpURLConnection connection, long deadline) {
        synchronized (lifecycleLock) {
            if (!activeLocked(generation) || clock.now() >= deadline) return false;
            activeConnection = connection;
            long delay = Math.max(1L, deadline - clock.now());
            deadlineTask = deadlineExecutor.schedule(() -> expireConnection(generation, connection), delay, TimeUnit.MILLISECONDS);
            return true;
        }
    }

    /** The timer only closes this current connection; the poll thread emits its one fail-closed disconnect. */
    private void expireConnection(long generation, HttpURLConnection connection) {
        boolean disconnect = false;
        synchronized (lifecycleLock) {
            if (activeConnection == connection) {
                activeConnection = null;
                deadlineTask = null;
                disconnect = true;
            }
        }
        if (disconnect) connection.disconnect();
    }

    private boolean active(long generation) {
        synchronized (lifecycleLock) { return activeLocked(generation); }
    }

    private boolean activeLocked(long generation) { return !stopped && fence.current(generation); }

    private boolean currentConnection(long generation, HttpURLConnection connection) {
        synchronized (lifecycleLock) { return activeLocked(generation) && activeConnection == connection; }
    }

    private void requireCurrent(long generation, HttpURLConnection connection, long deadline) throws Exception {
        if (!currentConnection(generation, connection)) throw new InterruptedException("poll_stopped");
        if (clock.now() >= deadline) throw new java.io.IOException("response_deadline");
    }

    /** Ends a current generation and actively releases its HTTP call outside the lifecycle lock. */
    private boolean endGeneration(long generation, boolean requireGeneration) {
        ScheduledFuture<?> oldTask;
        ScheduledFuture<?> oldDeadline;
        HttpURLConnection connection;
        synchronized (lifecycleLock) {
            if (requireGeneration && !activeLocked(generation)) return false;
            if (!requireGeneration && stopped && task == null && activeConnection == null) return false;
            stopped = true;
            fence.invalidate();
            oldTask = task;
            oldDeadline = deadlineTask;
            connection = activeConnection;
            task = null;
            deadlineTask = null;
            activeConnection = null;
        }
        if (oldTask != null) oldTask.cancel(true);
        if (oldDeadline != null) oldDeadline.cancel(false);
        if (connection != null) connection.disconnect();
        return true;
    }

    private void stopRetiredGeneration() {
        ScheduledFuture<?> oldTask;
        synchronized (lifecycleLock) {
            if (stopped && task == null && activeConnection == null) return;
            stopped = true;
            fence.invalidate();
            oldTask = task;
            task = null;
            // Do not cancel deadlineTask or clear activeConnection here. Its watchdog bounds a late GET.
        }
        if (oldTask != null) oldTask.cancel(true);
    }

    private void clearConnection(HttpURLConnection connection) {
        if (connection == null) return;
        ScheduledFuture<?> oldDeadline = null;
        synchronized (lifecycleLock) {
            if (activeConnection == connection) {
                activeConnection = null;
                oldDeadline = deadlineTask;
                deadlineTask = null;
            }
        }
        if (oldDeadline != null) oldDeadline.cancel(false);
    }

    /** Package-visible for deterministic JVM regression tests; no external endpoint is needed. */
    static String read(InputStream input, long deadline, MonotonicClock clock, BooleanSupplier cancelled) throws Exception {
        try (InputStream stream = input; ByteArrayOutputStream output = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[512];
            int count;
            checkReadDeadline(deadline, clock, cancelled);
            while ((count = stream.read(buffer)) != -1) {
                if (output.size() + count > MAX_BYTES) throw new IllegalStateException("response_size");
                output.write(buffer, 0, count);
                checkReadDeadline(deadline, clock, cancelled);
            }
            // EOF may arrive after the final blocking read; it is not a license to emit a late candidate.
            checkReadDeadline(deadline, clock, cancelled);
            return output.toString(StandardCharsets.UTF_8.name());
        }
    }

    private static void checkReadDeadline(long deadline, MonotonicClock clock, BooleanSupplier cancelled) throws Exception {
        if (cancelled.getAsBoolean()) throw new InterruptedException("poll_stopped");
        if (clock.now() >= deadline) throw new java.io.IOException("response_deadline");
    }
}
