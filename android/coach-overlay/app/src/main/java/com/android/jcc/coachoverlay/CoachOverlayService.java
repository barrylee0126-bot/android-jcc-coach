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


/** A user-enabled local overlay renderer. It has no network or game-control code. */
public final class CoachOverlayService extends Service {
    static final String ACTION_SHOW_SYNTHETIC = "com.android.jcc.coachoverlay.SHOW_SYNTHETIC";
    static final String ACTION_CLEAR = "com.android.jcc.coachoverlay.CLEAR_OVERLAY";
    static final String ACTION_DISCONNECTED = "com.android.jcc.coachoverlay.SOURCE_DISCONNECTED";
    private static final String CHANNEL_ID = "coach-overlay";
    private static final int CARD_WIDTH_DP = 520;
    private static final float CARD_ALPHA = .70f;
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

    public static void showSynthetic(Context context) {
        Intent intent = new Intent(context, CoachOverlayService.class)
                .setAction(ACTION_SHOW_SYNTHETIC);
        context.startForegroundService(intent);
    }

    public static void clear(Context context) {
        context.startForegroundService(new Intent(context, CoachOverlayService.class).setAction(ACTION_CLEAR));
    }

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
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        latestStartId = startId;
        if (intent == null || ACTION_CLEAR.equals(intent.getAction()) || ACTION_DISCONNECTED.equals(intent.getAction())) {
            renderCurrent(session.clear());
        } else if (ACTION_SHOW_SYNTHETIC.equals(intent.getAction())) {
            renderCurrent(session.show(SystemClock.elapsedRealtime()));
        } else {
            renderCurrent(session.clear());
        }
        return START_NOT_STICKY;
    }

    /** All state is volatile in this synthetic-only prototype. */
    private void renderCurrent(OverlayState.Render value) {
        if (value == null) return;
        handler.removeCallbacksAndMessages(null);
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
        handler.postAtTime(() -> renderCurrent(session.expiry(scheduledGeneration,
                SystemClock.elapsedRealtime())), SystemClock.uptimeMillis() + remaining);
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
        handler.removeCallbacksAndMessages(null);
        removeCard();
        stopOverlayService(latestStartId);
        super.onTaskRemoved(rootIntent);
    }

    @Override public void onDestroy() {
        handler.removeCallbacksAndMessages(null);
        if (screenReceiverRegistered) {
            unregisterReceiver(screenOn);
            screenReceiverRegistered = false;
        }
        session.clear();
        removeCard();
        super.onDestroy();
    }

    @Override public IBinder onBind(Intent intent) { return null; }
}
