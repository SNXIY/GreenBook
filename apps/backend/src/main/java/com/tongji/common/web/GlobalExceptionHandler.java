package com.tongji.common.web;

import com.tongji.agentfacade.api.dto.AgentErrorResponse;
import com.tongji.agentfacade.contract.DraftMetadataContract;
import com.tongji.common.exception.BusinessException;
import com.tongji.common.exception.ErrorCode;
import com.tongji.common.trace.TraceContextFilter;
import jakarta.validation.ConstraintViolationException;
import lombok.extern.slf4j.Slf4j;
import org.springframework.dao.DataIntegrityViolationException;
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
                ec.getCode(), ex.getMessage(), safeUserMessage(ec),
                ec == ErrorCode.DEPENDENCY_UNAVAILABLE,
                false,
                traceId());
        log.warn("Business error: code={}, traceId={}", ec.getCode(), body.traceId());
        return ResponseEntity.status(status).body(body);
    }

    @ExceptionHandler(MethodArgumentNotValidException.class)
    public ResponseEntity<AgentErrorResponse> handleValidation(MethodArgumentNotValidException ex) {
        FieldError error = ex.getBindingResult().getFieldErrors().stream().findFirst().orElse(null);
        if (error != null && isSizeViolation(error)) {
            int actualLength = error.getRejectedValue() == null
                    ? 0 : codePointLength(String.valueOf(error.getRejectedValue()));
            AgentErrorResponse body = new AgentErrorResponse(
                    ErrorCode.FIELD_TOO_LONG.getCode(),
                    "field=" + error.getField() + "; maxLength="
                            + DraftMetadataContract.DESCRIPTION_MAX_LENGTH + "; actualLength=" + actualLength,
                    "生成的草稿信息不符合发布要求，系统无法保存草稿。",
                    false, false, traceId(), error.getField(),
                    DraftMetadataContract.DESCRIPTION_MAX_LENGTH, actualLength, null);
            return ResponseEntity.badRequest().body(body);
        }
        String message = error != null && error.getDefaultMessage() != null
                ? error.getDefaultMessage() : ErrorCode.BAD_REQUEST.getDefaultMessage();
        return ResponseEntity.badRequest().body(new AgentErrorResponse(
                ErrorCode.VALIDATION_ERROR.getCode(), message, "请求参数不符合要求。",
                false, false, traceId()));
    }

    @ExceptionHandler(DataIntegrityViolationException.class)
    public ResponseEntity<AgentErrorResponse> handleDataIntegrity(DataIntegrityViolationException ex) {
        if (containsDescriptionTruncation(ex)) {
            return descriptionTooLongResponse();
        }
        return internalErrorResponse(ex);
    }

    @ExceptionHandler(ConstraintViolationException.class)
    public ResponseEntity<AgentErrorResponse> handleConstraintViolation(ConstraintViolationException ex) {
        return ResponseEntity.badRequest().body(new AgentErrorResponse(
                ErrorCode.VALIDATION_ERROR.getCode(), "Constraint validation failed",
                "请求参数不符合要求。", false, false, traceId()));
    }

    @ExceptionHandler(AccessDeniedException.class)
    public ResponseEntity<AgentErrorResponse> handleAccessDenied(AccessDeniedException ex) {
        return ResponseEntity.status(HttpStatus.FORBIDDEN).body(new AgentErrorResponse(
                ErrorCode.FORBIDDEN.getCode(), "Access denied", "无权执行该操作。",
                false, false, traceId()));
    }

    @ExceptionHandler(ResponseStatusException.class)
    public ResponseEntity<AgentErrorResponse> handleResponseStatus(ResponseStatusException ex) {
        return ResponseEntity.status(ex.getStatusCode()).body(new AgentErrorResponse(
                ErrorCode.INTERNAL_ERROR.getCode(), ex.getReason(), "服务暂时无法完成这项操作。",
                false, false, traceId()));
    }

    @ExceptionHandler(Exception.class)
    public ResponseEntity<AgentErrorResponse> handleGeneric(Exception ex) {
        if (containsDescriptionTruncation(ex)) {
            return descriptionTooLongResponse();
        }
        return internalErrorResponse(ex);
    }

    private ResponseEntity<AgentErrorResponse> descriptionTooLongResponse() {
        log.warn("Rejected description at persistence boundary, traceId={}", traceId());
        return ResponseEntity.badRequest().body(new AgentErrorResponse(
                ErrorCode.FIELD_TOO_LONG.getCode(),
                "field=description; maxLength=" + DraftMetadataContract.DESCRIPTION_MAX_LENGTH,
                "生成的草稿信息不符合发布要求，系统无法保存草稿。",
                false, false, traceId(), "description",
                DraftMetadataContract.DESCRIPTION_MAX_LENGTH, null, null));
    }

    private ResponseEntity<AgentErrorResponse> internalErrorResponse(Exception ex) {
        log.error("Unhandled backend exception, traceId={}", traceId(), ex);
        return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR).body(new AgentErrorResponse(
                ErrorCode.INTERNAL_ERROR.getCode(), "Internal backend failure",
                "服务暂时无法完成这项操作。", false, false, traceId()));
    }

    private static boolean isSizeViolation(FieldError error) {
        for (String code : error.getCodes() == null ? new String[0] : error.getCodes()) {
            if (code != null && code.startsWith("Size")) return true;
        }
        return false;
    }

    private static boolean containsDescriptionTruncation(Throwable error) {
        for (Throwable current = error; current != null; current = current.getCause()) {
            String message = current.getMessage();
            if (message != null && (message.contains("description")
                    && (message.contains("Data too long") || message.contains("DataTruncation")))) {
                return true;
            }
        }
        return false;
    }

    private static int codePointLength(String value) {
        return value.codePointCount(0, value.length());
    }

    private static String safeUserMessage(ErrorCode code) {
        return switch (code) {
            case FIELD_TOO_LONG, INVALID_DRAFT_METADATA -> "生成的草稿信息不符合发布要求，系统无法保存草稿。";
            case DEPENDENCY_UNAVAILABLE -> "社区服务暂时不可用，请稍后再试。";
            default -> code.getDefaultMessage();
        };
    }
}
