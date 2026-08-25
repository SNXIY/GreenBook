package com.tongji.search;

public final class SearchProviderUnavailableException extends SearchProviderException {
    public SearchProviderUnavailableException(String message) { super(message); }
    public SearchProviderUnavailableException(String message, Throwable cause) { super(message, cause); }
}
