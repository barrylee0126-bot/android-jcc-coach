package com.android.jcc.coachoverlay;

import static org.junit.Assert.assertTrue;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

import org.junit.Test;

/** JVM scheduling proof only; it does not instantiate Android Service or measure Android HttpURLConnection. */
public final class CoachOverlayServiceLifecycleTest {
    @Test public void callerReturnsBeforeControlledDisconnectIsReleased() throws Exception {
        ExecutorService closeExecutor = Executors.newSingleThreadExecutor();
        CountDownLatch closeStarted = new CountDownLatch(1);
        CountDownLatch releaseClose = new CountDownLatch(1);
        CountDownLatch callerReturned = new CountDownLatch(1);
        Thread caller = new Thread(() -> {
            CoachOverlayService.closeOffMain(closeExecutor, () -> {
                closeStarted.countDown();
                try { releaseClose.await(); } catch (InterruptedException ignored) { Thread.currentThread().interrupt(); }
            });
            callerReturned.countDown();
        }, "main-path-model");
        try {
            caller.start();
            assertTrue(closeStarted.await(2, TimeUnit.SECONDS));
            assertTrue("the caller must not wait for a slow disconnect", callerReturned.await(200, TimeUnit.MILLISECONDS));
        } finally {
            releaseClose.countDown();
            closeExecutor.shutdownNow();
            caller.join(2_000L);
        }
    }
}
