package com.android.jcc.coachoverlay;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.provider.Settings;
import android.text.InputType;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;


/** Explicit user controls for local synthetic display checks; no game access. */
public final class MainActivity extends Activity {
    private TextView state;
    private EditText loopbackPort;

    @Override public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        int padding = dp(20);
        root.setPadding(padding, padding, padding, padding);
        state = new TextView(this);
        Button permission = new Button(this);
        permission.setText(R.string.permission_button);
        permission.setOnClickListener(view -> requestOverlayPermission());
        Button show = new Button(this);
        show.setText(R.string.show_button);
        show.setOnClickListener(view -> CoachOverlayService.showSynthetic(this));
        Button clear = new Button(this);
        clear.setText(R.string.clear_button);
        clear.setOnClickListener(view -> CoachOverlayService.clear(this));
        loopbackPort = new EditText(this);
        loopbackPort.setInputType(InputType.TYPE_CLASS_NUMBER);
        loopbackPort.setHint(R.string.loopback_port_hint);
        loopbackPort.setText(Integer.toString(CoachOverlayService.DEFAULT_LOOPBACK_PORT));
        Button loopback = new Button(this);
        loopback.setText(R.string.loopback_button);
        loopback.setOnClickListener(view -> startLoopbackFromInput());
        Button stop = new Button(this);
        stop.setText(R.string.stop_loopback_button);
        stop.setOnClickListener(view -> CoachOverlayService.stopLoopback(this));
        root.addView(state, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT));
        root.addView(permission);
        root.addView(show);
        root.addView(clear);
        root.addView(loopbackPort);
        root.addView(loopback);
        root.addView(stop);
        setContentView(root);
    }

    @Override protected void onResume() {
        super.onResume();
        int permission = Settings.canDrawOverlays(this)
                ? R.string.permission_granted : R.string.permission_missing;
        state.setText(getString(R.string.permission_and_session_status, getString(permission)));
    }

    private void requestOverlayPermission() {
        Intent intent = new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                Uri.parse("package:" + getPackageName()));
        startActivity(intent);
    }

    private void startLoopbackFromInput() {
        int port = parsePort(loopbackPort.getText().toString());
        if (port == 0) {
            state.setText(R.string.loopback_port_invalid);
            return;
        }
        // CoachOverlayService fixes the scheme/host/path to 127.0.0.1:/api/view.
        CoachOverlayService.startLoopback(this, port);
    }

    static int parsePort(String raw) {
        if (raw == null || raw.length() < 4 || raw.length() > 5) return 0;
        int value = 0;
        for (int index = 0; index < raw.length(); index++) {
            char character = raw.charAt(index);
            if (character < '0' || character > '9') return 0;
            value = value * 10 + (character - '0');
        }
        return value >= 1024 && value <= 65535 ? value : 0;
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
