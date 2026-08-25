package com.tongji.search.rag.projection;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;

/** Explicit opt-in operator hook for a one-shot live chunk backfill. */
@Component
@ConditionalOnProperty(name = "rag.rebuild-on-start", havingValue = "true")
public class PostChunkRebuildStartup implements ApplicationRunner {
    private static final Logger log = LoggerFactory.getLogger(PostChunkRebuildStartup.class);
    private final PostChunkRebuildService rebuild;

    public PostChunkRebuildStartup(PostChunkRebuildService rebuild) {
        this.rebuild = rebuild;
    }

    @Override
    public void run(ApplicationArguments args) {
        int count = rebuild.rebuildPublic(25);
        log.info("RAG chunk rebuild completed posts={}", count);
    }
}
