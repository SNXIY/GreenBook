package com.tongji.notification.api.dto;

import java.util.List;

public record MarkReadRequest(List<String> ids) {
}
