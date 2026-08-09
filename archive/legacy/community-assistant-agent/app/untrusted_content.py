from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_CONTROL_CHARS = re.compile(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_SIGNATURES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ROLE_OVERRIDE",
        re.compile(
            r"(忽略|无视).{0,16}(之前|以上|系统).{0,12}(指令|规则)|"
            r"ignore.{0,20}(previous|system).{0,12}instructions?",
            re.IGNORECASE,
        ),
    ),
    (
        "SECRET_EXFILTRATION",
        re.compile(
            r"(输出|泄露|发送|显示).{0,18}(token|api.?key|密钥|系统提示词)|"
            r"(reveal|send|print).{0,18}(token|secret|system prompt)",
            re.IGNORECASE,
        ),
    ),
    (
        "TOOL_INJECTION",
        re.compile(
            r"(调用|执行).{0,12}(工具|函数|接口)|"
            r"(call|execute).{0,12}(tool|function|api)",
            re.IGNORECASE,
        ),
    ),
    (
        "ROLE_MARKUP",
        re.compile(
            r"<\|?(system|assistant|developer)\|?>|"
            r"^\s*(system|developer)\s*:",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
)


@dataclass(frozen=True)
class UntrustedText:
    text: str
    signals: tuple[str, ...]


def inspect_untrusted_text(value: Any, *, max_chars: int) -> UntrustedText:
    text = str(value or "").replace("\r\n", "\n")
    text = _CONTROL_CHARS.sub(" ", text)
    hidden_comments = bool(_HTML_COMMENT.search(text))
    text = _HTML_COMMENT.sub(" ", text)
    signals = [name for name, pattern in _SIGNATURES if pattern.search(text)]
    if hidden_comments:
        signals.append("HIDDEN_HTML_COMMENT")
    return UntrustedText(
        text=text[:max_chars],
        signals=tuple(dict.fromkeys(signals)),
    )


def guard_post_payload(post: dict[str, Any]) -> dict[str, Any]:
    guarded = dict(post)
    body_key = "body_markdown" if "body_markdown" in guarded else "bodyMarkdown"
    body = inspect_untrusted_text(guarded.get(body_key), max_chars=524_288)
    title = inspect_untrusted_text(guarded.get("title"), max_chars=256)
    description = inspect_untrusted_text(
        guarded.get("description"),
        max_chars=1_000,
    )
    guarded[body_key] = body.text
    guarded["title"] = title.text
    if guarded.get("description") is not None:
        guarded["description"] = description.text
    signals = tuple(
        dict.fromkeys((*title.signals, *description.signals, *body.signals))
    )
    guarded["untrusted_content"] = True
    guarded["injection_signals"] = list(signals)
    return guarded
