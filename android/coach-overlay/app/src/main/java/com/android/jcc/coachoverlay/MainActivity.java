package com.android.jcc.coachoverlay;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.provider.Settings;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;


/** Explicit user controls for local synthetic display checks; no game access. */
public final class MainActivity extends Activity {
    private TextView state;

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
        root.addView(state, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT));
        root.addView(permission);
        root.addView(show);
        root.addView(clear);
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

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
