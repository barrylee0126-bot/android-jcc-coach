package com.android.jcc.coachoverlay;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.graphics.Color;
import android.graphics.PixelFormat;
import android.graphics.drawable.GradientDrawable;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.SystemClock;
import android.provider.Settings;
import android.text.TextPaint;
import android.view.Gravity;
import android.view.WindowManager;
import android.widget.TextView;

import java.util.concurrent.Executors;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executor;
import java.util.concurrent.ScheduledExecutorService;


/** A user-enabled local overlay renderer. It has no game-control code and only reads an explicit loopback endpoint. */
public final class CoachOverlayService extends Service {
    static final String ACTION_SHOW_SYNTHETIC = "com.android.jcc.coachoverlay.SHOW_SYNTHETIC";
    static final String ACTION_CLEAR = "com.android.jcc.coachoverlay.CLEAR_OVERLAY";
    static final String ACTION_DISCONNECTED = "com.android.jcc.coachoverlay.SOURCE_DISCONNECTED";
    static final String ACTION_START_LOOPBACK = "com.android.jcc.coachoverlay.START_LOOPBACK";
    static final String ACTION_STOP_LOOPBACK = "com.android.jcc.coachoverlay.STOP_LOOPBACK";
    /** Internal to this non-exported Service; MainActivity never accepts it from an external Intent. */
    static final String EXTRA_LOOPBACK_PORT = "loopback_port";
    static final int DEFAULT_LOOPBACK_PORT = 18181;
    private static final String CHANNEL_ID = "coach-overlay";
    private static final int CARD_WIDTH_DP = 520;
    private static final float CARD_ALPHA = .70f;
    private static final long RETIRED_READER_CLEANUP_MS = 1_000L;
    private SyntheticOverlaySession session;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final BroadcastReceiver screenOn = new BroadcastReceiver() {
        @Override public void onReceive(Context context, Intent intent) {
            renderCurrent(session.current(SystemClock.elapsedRealtime()));
        }
    };
    private WindowManager windowManager;
    private TextView card;
    private boolean screenReceiverRegistered;
    private int latestStartId;
    private final ConnectionGeneration loopbackFence = new ConnectionGeneration();
    private LoopbackViewPoller loopbackPoller;
    private ReaderResources loopbackResources;
    /** Process-wide: prior Service instances may still own a reader after onDestroy returns. */
    private final RetiredReaderPool retiredReaders = ProcessRetiredReaderRegistry.pool();
    private boolean retiredReapScheduled;
    private Runnable expiry;
    /** Main-thread owned: synthetic demo and loopback observation never share a display session. */
    private boolean observing;

    public static void showSynthetic(Context context) {
        Intent intent = new Intent(context, CoachOverlayService.class)
                .setAction(ACTION_SHOW_SYNTHETIC);
        context.startForegroundService(intent);
    }

    public static void clear(Context context) {
        context.startForegroundService(new Intent(context, CoachOverlayService.class).setAction(ACTION_CLEAR));
    }

    public static void startLoopback(Context context) { startLoopback(context, DEFAULT_LOOPBACK_PORT); }
    public static void startLoopback(Context context, int port) { if (port < 1024 || port > 65535) return; context.startForegroundService(new Intent(context, CoachOverlayService.class).setAction(ACTION_START_LOOPBACK).putExtra(EXTRA_LOOPBACK_PORT, port)); }
    public static void stopLoopback(Context context) { context.startForegroundService(new Intent(context, CoachOverlayService.class).setAction(ACTION_STOP_LOOPBACK)); }

    /** Schedules a potentially blocking platform disconnect without making the main Service path wait. */
    static void closeOffMain(Executor executor, Runnable close) { executor.execute(close); }

    @Override public void onCreate() {
        super.onCreate();
        session = new SyntheticOverlaySession();
        windowManager = (WindowManager) getSystemService(WINDOW_SERVICE);
        createChannel();
        Notification notification = new Notification.Builder(this, CHANNEL_ID)
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .setContentTitle(getString(R.string.notification_title))
                .setContentText(getString(R.string.notification_text))
                .setOngoing(true)
                .build();
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(1, notification, android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        } else {
            startForeground(1, notification);
        }
        IntentFilter filter = new IntentFilter(Intent.ACTION_SCREEN_ON);
        if (Build.VERSION.SDK_INT >= 33) registerReceiver(screenOn, filter, Context.RECEIVER_NOT_EXPORTED);
        else registerReceiver(screenOn, filter);
        screenReceiverRegistered = true;
        if (!retiredReaders.isEmpty()) scheduleRetiredReap();
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        latestStartId = startId;
        if (intent == null || ACTION_CLEAR.equals(intent.getAction()) || ACTION_DISCONNECTED.equals(intent.getAction())) {
            if (intent == null || ACTION_DISCONNECTED.equals(intent.getAction())) stopLoopbackReader();
            renderCurrent(session.clear());
        } else if (ACTION_START_LOOPBACK.equals(intent.getAction())) {
            // An explicit new start changes mode, invalidates old callbacks, and starts from an empty card.
            stopLoopbackReader();
            renderCurrent(session.resetForNewObservation());
            startLoopbackReader(intent.getIntExtra(EXTRA_LOOPBACK_PORT, DEFAULT_LOOPBACK_PORT));
        } else if (ACTION_STOP_LOOPBACK.equals(intent.getAction())) {
            stopLoopbackReader(); renderCurrent(session.clear());
        } else if (ACTION_SHOW_SYNTHETIC.equals(intent.getAction())) {
            // A synthetic tap is a mode switch, not an event interleaved with an observation reader.
            if (observing) stopLoopbackReader();
            renderCurrent(session.show(SystemClock.elapsedRealtime()));
        } else {
            renderCurrent(session.clear());
        }
        return START_NOT_STICKY;
    }

    private void startLoopbackReader(int port) {
        if (port < 1024 || port > 65535) {
            renderCurrent(session.clear());
            return;
        }
        if (loopbackPoller != null || retiredReaders.atCapacity()) {
            renderCurrent(session.clear());
            return;
        }
        observing = true;
        long generation = loopbackFence.open();
        ReaderResources resources = new ReaderResources();
        loopbackResources = resources;
        loopbackPoller = new LoopbackViewPoller(resources.pollExecutor, resources.deadlineExecutor, new LoopbackViewPoller.Listener() {
            @Override public void onEnvelope(long source, String envelope) { handler.post(() -> {
                if (!loopbackFence.current(generation) || resources != loopbackResources) return;
                renderCurrent(session.accept(envelope, SystemClock.elapsedRealtime()));
            }); }
            @Override public void onDisconnected(long source) { handler.post(() -> {
                if (!loopbackFence.current(generation) || resources != loopbackResources) return;
                stopLoopbackReader();
                renderCurrent(session.clear());
            }); }
        }, port);
        loopbackPoller.start();
    }

    private void stopLoopbackReader() {
        loopbackFence.invalidate();
        observing = false;
        ReaderResources resources = loopbackResources;
        loopbackResources = null;
        if (loopbackPoller != null) {
            // stop() only invalidates local generations. A potentially slow platform disconnect runs off-main.
            loopbackPoller.stop();
            if (resources != null) resources.closeAsync(loopbackPoller);
        }
        loopbackPoller = null;
        if (resources != null) retire(resources);
    }

    /** An old HTTP stack may ignore an interrupt briefly; its own executor cannot occupy the new reader. */
    private void retire(ReaderResources resources) {
        if (!retiredReaders.retire(resources)) {
            // This should be unreachable because an at-capacity state cannot create another active reader.
            resources.requestShutdown();
            return;
        }
        scheduleRetiredReap();
    }

    private void scheduleRetiredReap() {
        if (retiredReapScheduled) return;
        retiredReapScheduled = true;
        handler.postDelayed(() -> {
            retiredReapScheduled = false;
            retiredReaders.reap();
            if (!retiredReaders.isEmpty()) scheduleRetiredReap();
        }, RETIRED_READER_CLEANUP_MS);
    }

    private static final class ReaderResources implements RetiredReaderPool.Resource {
        final ScheduledExecutorService pollExecutor = Executors.newSingleThreadScheduledExecutor();
        final ScheduledExecutorService deadlineExecutor = Executors.newSingleThreadScheduledExecutor();
        final ExecutorService closeExecutor = Executors.newSingleThreadExecutor();
        void closeAsync(LoopbackViewPoller poller) {
            closeOffMain(closeExecutor, poller::closeConnection);
            closeExecutor.shutdown();
        }
        @Override public void requestShutdown() {
            pollExecutor.shutdownNow();
            // A close task may itself block; preserve both deadline and close executors until poll/close end.
            closeExecutor.shutdown();
            if (pollExecutor.isTerminated() && closeExecutor.isTerminated()) deadlineExecutor.shutdownNow();
        }
        @Override public boolean isTerminated() {
            return pollExecutor.isTerminated() && deadlineExecutor.isTerminated() && closeExecutor.isTerminated();
        }
    }

    /** All state is volatile in this synthetic-only prototype. */
    private void renderCurrent(OverlayState.Render value) {
        if (value == null) return;
        if (expiry != null) handler.removeCallbacks(expiry);
        if (!value.visible) {
            removeCard();
            return;
        }
        if (!Settings.canDrawOverlays(this)) {
            session.clear();
            removeCard();
            return;
        }
        if (card == null && !addCard(value)) {
            session.clear();
            removeCard();
            return;
        }
        if (!fitsEachLine(card.getPaint(), value)) {
            session.clear();
            removeCard();
            return;
        }
        card.setText(getString(R.string.overlay_card, value.titleZh, value.actionZh, value.reasonZh));
        long remaining = Math.max(1L, value.expiresAtElapsedMs - SystemClock.elapsedRealtime());
        long scheduledGeneration = session.generation();
        expiry = () -> renderCurrent(session.expiry(scheduledGeneration, SystemClock.elapsedRealtime()));
        handler.postAtTime(expiry, SystemClock.uptimeMillis() + remaining);
    }

    private boolean addCard(OverlayState.Render value) {
        TextView view = new TextView(this);
        view.setTextColor(Color.WHITE);
        view.setTextSize(14);
        view.setMaxLines(3);
        view.setPadding(dp(14), dp(9), dp(14), dp(9));
        if (!fitsEachLine(view.getPaint(), value)) return false;
        GradientDrawable background = new GradientDrawable();
        background.setColor(Color.rgb(24, 43, 62));
        background.setCornerRadius(dp(12));
        background.setStroke(dp(1), Color.rgb(86, 137, 175));
        view.setBackground(background);
        WindowManager.LayoutParams params = new WindowManager.LayoutParams(
                dp(CARD_WIDTH_DP), WindowManager.LayoutParams.WRAP_CONTENT,
                WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                        | WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE
                        | WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
                PixelFormat.TRANSLUCENT);
        params.gravity = Gravity.TOP | Gravity.CENTER_HORIZONTAL;
        params.y = dp(48);
        // This stays below Android's documented 0.8 opacity threshold, but still needs device testing.
        params.alpha = CARD_ALPHA;
        try {
            windowManager.addView(view, params);
            card = view;
            return true;
        } catch (RuntimeException ignored) {
            card = null;
            return false;
        }
    }

    private boolean fitsEachLine(TextPaint paint, OverlayState.Render value) {
        float available = dp(CARD_WIDTH_DP - 28);
        return paint.measureText(value.titleZh) <= available && paint.measureText(value.actionZh) <= available
                && paint.measureText(value.reasonZh) <= available;
    }

    private void removeCard() {
        if (card != null) {
            try { windowManager.removeView(card); } catch (RuntimeException ignored) { }
            card = null;
        }
    }

    private void stopOverlayService(int startId) {
        if (stopSelfResult(startId)) stopForeground(STOP_FOREGROUND_REMOVE);
    }

    private void createChannel() {
        NotificationChannel channel = new NotificationChannel(CHANNEL_ID,
                getString(R.string.notification_channel), NotificationManager.IMPORTANCE_LOW);
        getSystemService(NotificationManager.class).createNotificationChannel(channel);
    }

    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }

    @Override public void onTaskRemoved(Intent rootIntent) {
        session.clear();
        stopLoopbackReader();
        if (expiry != null) handler.removeCallbacks(expiry);
        removeCard();
        stopOverlayService(latestStartId);
        super.onTaskRemoved(rootIntent);
    }

    @Override public void onDestroy() {
        if (expiry != null) handler.removeCallbacks(expiry);
        if (screenReceiverRegistered) {
            unregisterReceiver(screenOn);
            screenReceiverRegistered = false;
        }
        session.clear();
        stopLoopbackReader();
        retiredReaders.requestShutdownAll();
        removeCard();
        super.onDestroy();
    }

    @Override public IBinder onBind(Intent intent) { return null; }
}
