package com.tongji.notification.api;

import com.tongji.auth.token.JwtService;
import com.tongji.notification.api.dto.MarkReadRequest;
import com.tongji.notification.api.dto.NotificationPageResponse;
import com.tongji.notification.api.dto.UnreadCountResponse;
import com.tongji.notification.service.NotificationService;
import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PatchMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/v1/notifications")
@RequiredArgsConstructor
public class NotificationController {
    private final NotificationService notificationService;
    private final JwtService jwtService;

    @GetMapping
    public NotificationPageResponse list(@RequestParam(value = "cursor", required = false) Long cursor,
                                         @RequestParam(value = "size", defaultValue = "20") int size,
                                         @AuthenticationPrincipal Jwt jwt) {
        return notificationService.list(jwtService.extractUserId(jwt), cursor, size);
    }

    @GetMapping("/unread-count")
    public UnreadCountResponse unreadCount(@AuthenticationPrincipal Jwt jwt) {
        return new UnreadCountResponse(notificationService.unreadCount(jwtService.extractUserId(jwt)));
    }

    @PostMapping("/read")
    public ResponseEntity<Void> markRead(@Valid @RequestBody MarkReadRequest request,
                                         @AuthenticationPrincipal Jwt jwt) {
        notificationService.markRead(jwtService.extractUserId(jwt), request.ids());
        return ResponseEntity.noContent().build();
    }

    @PatchMapping("/read-all")
    public ResponseEntity<Void> markAllRead(@AuthenticationPrincipal Jwt jwt) {
        notificationService.markAllRead(jwtService.extractUserId(jwt));
        return ResponseEntity.noContent().build();
    }
}
