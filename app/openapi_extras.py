"""Add the chat SSE event models to the OpenAPI schema, so the web app gets their types."""

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from pydantic import TypeAdapter

from app.schemas.chat import ChatStreamEvent

REF_TEMPLATE = "#/components/schemas/{model}"


def build_openapi(app: FastAPI) -> dict[str, Any]:
    """FastAPI's schema plus `ChatStreamEvent` (a union of the four SSE payloads)."""
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    event_schema = TypeAdapter(ChatStreamEvent).json_schema(ref_template=REF_TEMPLATE)
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    components.update(event_schema.pop("$defs", {}))
    components["ChatStreamEvent"] = event_schema
    app.openapi_schema = schema
    return schema
