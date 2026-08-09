package com.tongji.common.web;

import com.tongji.agentfacade.api.dto.AgentErrorResponse;
import com.tongji.common.exception.BusinessException;
import com.tongji.common.exception.ErrorCode;
import com.tongji.common.trace.TraceContextFilter;
import jakarta.validation.ConstraintViolationException;
import lombok.extern.slf4j.Slf4j;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.AccessDeniedException;
import org.springframework.validation.FieldError;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.server.ResponseStatusException;

@Slf4j
@RestControllerAdvice
public class GlobalExceptionHandler {

    private String traceId() {
        return TraceContextFilter.currentOrCreate();
    }

    @ExceptionHandler(BusinessException.class)
    public ResponseEntity<AgentErrorResponse> handleBusiness(BusinessException ex) {
        ErrorCode ec = ex.getErrorCode();
        HttpStatus status = switch (ec) {
            case UNAUTHORIZED, AUTHENTICATION_REQUIRED -> HttpStatus.UNAUTHORIZED;
            case FORBIDDEN -> HttpStatus.FORBIDDEN;
            case NOT_FOUND -> HttpStatus.NOT_FOUND;
            case CONFLICT, IDEMPOTENCY_CONFLICT -> HttpStatus.CONFLICT;
            case INTERNAL_ERROR -> HttpStatus.INTERNAL_SERVER_ERROR;
            case DEPENDENCY_UNAVAILABLE -> HttpStatus.SERVICE_UNAVAILABLE;
            default -> HttpStatus.BAD_REQUEST;
        };
        AgentErrorResponse body = new AgentErrorResponse(
                ec.getCode(), ex.getMessage(), ex.getMessage(),
                ec == ErrorCode.DEPENDENCY_UNAVAILABLE,
                ec != ErrorCode.INTERNAL_ERROR,
                traceId());
        log.warn("Business error: code={}, msg={}, traceId={}", ec.getCode(), ex.getMessage(), body.traceId());
        return ResponseEntity.status(status).body(body);
    }

    @ExceptionHandler(MethodArgumentNotValidException.class)
    public ResponseEntity<AgentErrorResponse> handleValidation(MethodArgumentNotValidException ex) {
        String message = ex.getBindingResult().getFieldErrors().stream()
                .findFirst()
                .map(FieldError::getDefaultMessage)
                .orElse(ErrorCode.BAD_REQUEST.getDefaultMessage());
        AgentErrorResponse body = new AgentErrorResponse(
                "VALIDATION_ERROR", message, message,
                false, false, traceId());
        return ResponseEntity.status(HttpStatus.BAD_REQUEST).body(body);
    }

    @ExceptionHandler(ConstraintViolationException.class)
    public ResponseEntity<AgentErrorResponse> handleConstraintViolation(ConstraintViolationException ex) {
        AgentErrorResponse body = new AgentErrorResponse(
                "VALIDATION_ERROR", ex.getMessage(), ex.getMessage(),
                false, false, traceId());
        return ResponseEntity.status(HttpStatus.BAD_REQUEST).body(body);
    }

    @ExceptionHandler(AccessDeniedException.class)
    public ResponseEntity<AgentErrorResponse> handleAccessDenied(AccessDeniedException ex) {
        AgentErrorResponse body = new AgentErrorResponse(
                "FORBIDDEN", ex.getMessage(), "无权执行该操作",
                false, false, traceId());
        return ResponseEntity.status(HttpStatus.FORBIDDEN).body(body);
    }

    @ExceptionHandler(ResponseStatusException.class)
    public ResponseEntity<AgentErrorResponse> handleResponseStatus(ResponseStatusException ex) {
        AgentErrorResponse body = new AgentErrorResponse(
                "INTERNAL_ERROR", ex.getReason(), "服务异常",
                ex.getStatusCode().is5xxServerError(),
                false, traceId());
        return ResponseEntity.status(ex.getStatusCode()).body(body);
    }

    @ExceptionHandler(Exception.class)
    public ResponseEntity<AgentErrorResponse> handleGeneric(Exception ex) {
        log.error("Unhandled exception", ex);
        AgentErrorResponse body = new AgentErrorResponse(
                "INTERNAL_ERROR", ex.getMessage() != null ? ex.getMessage() : "未知错误",
                "服务异常，请稍后重试",
                true, false, traceId());
        return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR).body(body);
    }
}
