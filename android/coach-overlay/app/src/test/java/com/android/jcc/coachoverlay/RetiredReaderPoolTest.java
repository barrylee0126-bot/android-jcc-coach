package com.android.jcc.coachoverlay;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

import org.junit.Test;

/** Deterministic accounting: interruption is a request, not proof that a reader thread has terminated. */
public final class RetiredReaderPoolTest {
    @Test public void capacityRemainsConsumedUntilBothExecutorsActuallyTerminate() {
        RetiredReaderPool pool = new RetiredReaderPool(2);
        FakeResource first = new FakeResource();
        FakeResource second = new FakeResource();
        FakeResource third = new FakeResource();
        assertTrue(pool.retire(first));
        assertTrue(pool.retire(second));
        assertTrue(pool.atCapacity());
        assertFalse(pool.retire(third));

        pool.reap();
        assertEquals(2, pool.size());
        assertTrue(first.shutdownRequested);
        assertTrue(second.shutdownRequested);
        assertTrue(pool.atCapacity());

        first.terminated = true;
        pool.reap();
        assertEquals(1, pool.size());
        assertFalse(pool.atCapacity());
        assertTrue(pool.retire(third));
    }

    @Test public void terminatedResourceRestoresCapacityWithoutBlockingCaller() {
        RetiredReaderPool pool = new RetiredReaderPool(1);
        FakeResource resource = new FakeResource();
        assertTrue(pool.retire(resource));
        resource.terminated = true;
        pool.reap();
        assertTrue(pool.isEmpty());
        assertFalse(pool.atCapacity());
    }

    @Test public void shutdownNowDoesNotFreeCapacityWhileAWorkerIgnoresInterrupt() throws Exception {
        CountDownLatch started = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        ExecutorService executor = Executors.newSingleThreadExecutor();
        executor.execute(() -> {
            started.countDown();
            for (;;) {
                try {
                    release.await();
                    return;
                } catch (InterruptedException ignored) {
                    // Model a network implementation that does not stop merely because an interrupt was requested.
                }
            }
        });
        assertTrue(started.await(2, TimeUnit.SECONDS));
        RetiredReaderPool pool = new RetiredReaderPool(1);
        RetiredReaderPool.Resource resource = new RetiredReaderPool.Resource() {
            @Override public void requestShutdown() { executor.shutdownNow(); }
            @Override public boolean isTerminated() { return executor.isTerminated(); }
        };
        try {
            assertTrue(pool.retire(resource));
            pool.reap();
            assertTrue(pool.atCapacity());
            assertEquals(1, pool.size());
            release.countDown();
            assertTrue(executor.awaitTermination(2, TimeUnit.SECONDS));
            pool.reap();
            assertTrue(pool.isEmpty());
        } finally {
            release.countDown();
            executor.shutdownNow();
        }
    }

    @Test public void processWidePoolKeepsOldServiceReadersAcrossRebuildUntilTermination() {
        RetiredReaderPool firstService = ProcessRetiredReaderRegistry.pool();
        firstService.reap();
        assertTrue(firstService.isEmpty());
        FakeResource oldFirst = new FakeResource();
        FakeResource oldSecond = new FakeResource();
        try {
            assertTrue(firstService.retire(oldFirst));
            assertTrue(firstService.retire(oldSecond));
            // A new Service instance must see the same process-lifetime ownership pool.
            RetiredReaderPool rebuiltService = ProcessRetiredReaderRegistry.pool();
            assertTrue(rebuiltService.atCapacity());
            assertFalse(rebuiltService.retire(new FakeResource()));

            oldFirst.terminated = true;
            oldSecond.terminated = true;
            rebuiltService.reap();
            assertTrue(rebuiltService.isEmpty());
            assertFalse(rebuiltService.atCapacity());
        } finally {
            oldFirst.terminated = true;
            oldSecond.terminated = true;
            ProcessRetiredReaderRegistry.pool().reap();
        }
    }

    private static final class FakeResource implements RetiredReaderPool.Resource {
        boolean shutdownRequested;
        boolean terminated;
        @Override public void requestShutdown() { shutdownRequested = true; }
        @Override public boolean isTerminated() { return terminated; }
    }
}
