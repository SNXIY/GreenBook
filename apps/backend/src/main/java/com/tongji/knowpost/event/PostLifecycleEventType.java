package com.tongji.knowpost.event;

/** Canonical post mutations that can change a search projection. */
public enum PostLifecycleEventType {
    PostPublished,
    PostUpdated,
    PostDeleted,
    PostVisibilityChanged,
    PostContentUpdated
}
