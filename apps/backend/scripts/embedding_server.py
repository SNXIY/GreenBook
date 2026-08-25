"""Small local FastEmbed HTTP adapter for the Java retrieval provider.

The same model/tokenizer is used for query and post text. ``input_type`` is
accepted for an explicit contract, although the selected multilingual MiniLM
model does not require different query/document prefixes.
"""

from __future__ import annotations

import argparse
import json
import math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


class EmbeddingRuntime:
    def __init__(self, model_name: str, model_path: str | None, cache_dir: str,
                 version: str) -> None:
        from fastembed import TextEmbedding

        kwargs: dict[str, Any] = {"model_name": model_name, "cache_dir": cache_dir}
        if model_path:
            kwargs["specific_model_path"] = model_path
        self.model_name = model_name
        self.version = version
        self.model = TextEmbedding(**kwargs)

    def embed(self, text: str) -> list[float]:
        value = text if text.strip() else " "
        vector = next(self.model.embed([value]))
        values = [float(item) for item in vector]
        norm = math.sqrt(sum(item * item for item in values))
        if norm == 0.0:
            raise ValueError("embedding model returned a zero vector")
        return [item / norm for item in values]


class Handler(BaseHTTPRequestHandler):
    runtime: EmbeddingRuntime

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/health":
            self.send_error(404)
            return
        self._write(200, {
            "status": "UP",
            "embedding_model": self.runtime.model_name,
            "embedding_version": self.runtime.version,
        })

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/embed":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            text = body.get("text", "")
            if not isinstance(text, str):
                raise ValueError("text must be a string")
            vector = self.runtime.embed(text)
            self._write(200, {
                "embedding": vector,
                "embedding_model": self.runtime.model_name,
                "embedding_version": self.runtime.version,
                "dimension": len(vector),
                "normalized": True,
                "input_type": body.get("input_type", "unspecified"),
            })
        except Exception as error:  # the Java adapter turns non-2xx into provider failure
            self._write(503, {"error": str(error)})

    def log_message(self, format: str, *args: object) -> None:
        return

    def _write(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    parser.add_argument("--model-path", type=Path)
    parser.add_argument("--cache-dir", default=".retrieval-model-cache")
    parser.add_argument("--embedding-version", default="posts-dense-multilingual-v1")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8181)
    args = parser.parse_args()
    runtime = EmbeddingRuntime(args.model, str(args.model_path) if args.model_path else None,
                               args.cache_dir, args.embedding_version)
    Handler.runtime = runtime
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
