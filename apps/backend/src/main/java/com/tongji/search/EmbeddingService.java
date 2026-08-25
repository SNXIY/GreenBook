package com.tongji.search;

public interface EmbeddingService {
    float[] embed(String text);

    /** Query and document use the same model, tokenizer and normalization contract. */
    default float[] embedQuery(String text) {
        return embed(text);
    }

    default float[] embedDocument(String text) {
        return embed(text);
    }

    int dimension();
    String model();
    String vectorVersion();
}
