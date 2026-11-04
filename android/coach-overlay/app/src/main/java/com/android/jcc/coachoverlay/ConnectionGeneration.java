package com.android.jcc.coachoverlay;

/** Small synchronized fence for cancelling late I/O and UI callbacks deterministically. */
final class ConnectionGeneration {
    private long value;

    synchronized long open() { return ++value; }
    synchronized void invalidate() { ++value; }
    synchronized boolean current(long candidate) { return candidate == value; }
}
