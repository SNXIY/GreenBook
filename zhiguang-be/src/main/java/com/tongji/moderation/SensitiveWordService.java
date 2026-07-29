package com.tongji.moderation;

import java.util.List;

public interface SensitiveWordService {
    List<String> findAll(String... texts);

    default boolean contains(String... texts) {
        return !findAll(texts).isEmpty();
    }
}
