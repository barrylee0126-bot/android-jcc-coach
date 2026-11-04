package com.android.jcc.coachoverlay;

import java.util.ArrayList;
import java.util.Iterator;

/** Bounded, main-thread-owned accounting for readers that may ignore interruption for a while. */
final class RetiredReaderPool {
    interface Resource {
        void requestShutdown();
        boolean isTerminated();
    }

    private final int capacity;
    private final ArrayList<Resource> resources = new ArrayList<>();

    RetiredReaderPool(int capacity) { this.capacity = capacity; }

    synchronized boolean retire(Resource resource) {
        if (resources.size() >= capacity) return false;
        resources.add(resource);
        return true;
    }

    synchronized boolean atCapacity() { return resources.size() >= capacity; }
    synchronized boolean isEmpty() { return resources.isEmpty(); }
    synchronized int size() { return resources.size(); }

    /** Never blocks: request interruption, then free capacity only once the resource reports true termination. */
    void reap() {
        Resource[] snapshot;
        synchronized (this) { snapshot = resources.toArray(new Resource[0]); }
        for (Resource resource : snapshot) resource.requestShutdown();
        synchronized (this) {
            for (Iterator<Resource> iterator = resources.iterator(); iterator.hasNext();) {
                if (iterator.next().isTerminated()) iterator.remove();
            }
        }
    }

    void requestShutdownAll() {
        Resource[] snapshot;
        synchronized (this) { snapshot = resources.toArray(new Resource[0]); }
        for (Resource resource : snapshot) resource.requestShutdown();
    }
}
