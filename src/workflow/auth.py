"""Replaceable authentication boundary. No login endpoint or token issuance."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from pydantic import ValidationError

from src.workflow.errors import WorkflowError
from src.workflow.models import AuthContext


class Authenticator(Protocol):
    def authenticate(self, event: Mapping[str, object]) -> AuthContext: ...


class DevelopmentAuthenticator:
    def __init__(self, context: AuthContext) -> None:
        self.context = context

    def authenticate(self, event: Mapping[str, object]) -> AuthContext:
        # A fixed configured identity, never user/organization values from request headers.
        return self.context


class GatewayAuthenticator:
    def authenticate(self, event: Mapping[str, object]) -> AuthContext:
        context = event.get("requestContext")
        authorizer = context.get("authorizer") if isinstance(context, dict) else None
        if not isinstance(authorizer, dict):
            raise WorkflowError("E2001", "Authentication required", 401)
        # OCR's authorizer can emit JSON identity; its verification is outside this adapter.
        identity = authorizer.get("identity")
        try:
            if isinstance(identity, str):
                return AuthContext.model_validate_json(identity)
            return AuthContext.model_validate(identity)
        except ValidationError:
            raise WorkflowError("E2001", "Invalid authentication context", 401) from None
