package com.tongji.comment.api;

import com.tongji.auth.token.JwtService;
import com.tongji.comment.api.dto.CommentCreateRequest;
import com.tongji.comment.api.dto.CommentPageResponse;
import com.tongji.comment.api.dto.CommentResponse;
import com.tongji.comment.api.dto.CommentTopPatchRequest;
import com.tongji.comment.service.CommentService;
import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PatchMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

@RestController
@RequestMapping("/api/v1/comments")
@RequiredArgsConstructor
public class CommentController {
    private final CommentService commentService;
    private final JwtService jwtService;

    @PostMapping
    public CommentResponse create(@Valid @RequestBody CommentCreateRequest request,
                                  @AuthenticationPrincipal Jwt jwt) {
        long userId = jwtService.extractUserId(jwt);
        return commentService.create(userId, request);
    }

    @GetMapping
    public CommentPageResponse list(@RequestParam("postId") long postId,
                                    @RequestParam(value = "parentId", required = false) Long parentId,
                                    @RequestParam(value = "cursor", required = false) Long cursor,
                                    @RequestParam(value = "size", defaultValue = "20") int size,
                                    @AuthenticationPrincipal Jwt jwt) {
        Long userId = jwt == null ? null : jwtService.extractUserId(jwt);
        return commentService.list(postId, parentId, cursor, size, userId);
    }

    @GetMapping("/hot")
    public List<CommentResponse> hot(@RequestParam("postId") long postId,
                                     @RequestParam(value = "size", defaultValue = "5") int size,
                                     @AuthenticationPrincipal Jwt jwt) {
        Long userId = jwt == null ? null : jwtService.extractUserId(jwt);
        return commentService.hot(postId, size, userId);
    }

    @DeleteMapping("/{id}")
    public ResponseEntity<Void> delete(@PathVariable("id") long id,
                                       @AuthenticationPrincipal Jwt jwt) {
        long userId = jwtService.extractUserId(jwt);
        commentService.delete(userId, id);
        return ResponseEntity.noContent().build();
    }

    @PatchMapping("/{id}/top")
    public ResponseEntity<Void> top(@PathVariable("id") long id,
                                    @RequestBody CommentTopPatchRequest request,
                                    @AuthenticationPrincipal Jwt jwt) {
        long userId = jwtService.extractUserId(jwt);
        commentService.updateTop(userId, id, request.top());
        return ResponseEntity.noContent().build();
    }
}
