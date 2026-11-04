package com.android.jcc.coachoverlay;

import static org.junit.Assert.assertEquals;

import org.junit.Test;

/** The exported Activity accepts only a numeric loopback port; it never accepts a host or URL. */
public final class MainActivityPortTest {
    @Test public void onlyAsciiPortsInTheAllowedRangeAreAccepted() {
        assertEquals(18181, MainActivity.parsePort("18181"));
        assertEquals(1024, MainActivity.parsePort("1024"));
        assertEquals(65535, MainActivity.parsePort("65535"));
        assertEquals(0, MainActivity.parsePort("1023"));
        assertEquals(0, MainActivity.parsePort("65536"));
        assertEquals(0, MainActivity.parsePort("１８１８１"));
        assertEquals(0, MainActivity.parsePort("127.0.0.1"));
        assertEquals(0, MainActivity.parsePort("18181 "));
    }
}
