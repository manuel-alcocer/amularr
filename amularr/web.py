"""Small HTTP response container shared by the API modules."""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "text/plain; charset=utf-8"
    headers: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def text(cls, text: str, status: int = 200) -> "Response":
        return cls(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    @classmethod
    def json(cls, data, status: int = 200) -> "Response":
        return cls(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    @classmethod
    def xml(cls, data: bytes, status: int = 200) -> "Response":
        return cls(status, data, "application/xml; charset=utf-8")
