package com.android.jcc.coachoverlay;

/** Process-lifetime ownership: a rebuilt Service must still count readers retired by its predecessor. */
final class ProcessRetiredReaderRegistry {
    private static final RetiredReaderPool POOL = new RetiredReaderPool(2);
    private ProcessRetiredReaderRegistry() { }
    static RetiredReaderPool pool() { return POOL; }
}
