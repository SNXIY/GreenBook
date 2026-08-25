package com.tongji.search;

public class SearchProviderException extends RuntimeException {
    public SearchProviderException(String message) { super(message); }
    public SearchProviderException(String message, Throwable cause) { super(message, cause); }
}
