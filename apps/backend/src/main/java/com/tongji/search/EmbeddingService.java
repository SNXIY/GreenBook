package com.tongji.search;

public interface EmbeddingService {
    float[] embed(String text);
    int dimension();
    String model();
    String vectorVersion();
}
