"""Fail-closed bearer-token authorization for networked Harness pilots."""
from __future__ import annotations

import hmac
import os
import re
from dataclasses import dataclass
from pathlib import Path


class AuthenticationError(Exception):
    """Raised when a request has no currently valid identity."""


class AuthorizationError(Exception):
    """Raised when an authenticated identity lacks the required scope."""


@dataclass(frozen=True)
class Principal:
    name: str
    role: str
    scopes: frozenset[str]
    token_file: Path | None = None

    def allows(self, scope: str) -> bool:
        return "*" in self.scopes or scope in self.scopes


_PRINCIPAL_SPECS = (
    (
        "svc-panda-orchestrator",
        "orchestrator",
        "HARNESS_ORCHESTRATOR_TOKEN_FILE",
        frozenset({"tasks:create", "tasks:read", "operations:read"}),
    ),
    (
        "svc-hermes-nas",
        "hermes_nas",
        "HARNESS_NAS_WORKER_TOKEN_FILE",
        frozenset({
            "tasks:read",
            "tasks:claim",
            "tasks:heartbeat",
            "tasks:receipt",
        }),
    ),
)


class TokenAuthorizer:
    """Resolve bearer tokens from files on every request so revocation is immediate."""

    def __init__(self, principals: tuple[Principal, ...], enabled: bool) -> None:
        self.principals = principals
        self.enabled = enabled

    @classmethod
    def from_environment(cls) -> "TokenAuthorizer":
        required = os.environ.get("HARNESS_AUTH_REQUIRED", "0") == "1"
        principals: list[Principal] = []
        missing: list[str] = []
        for name, role, env_name, scopes in _PRINCIPAL_SPECS:
            configured = os.environ.get(env_name)
            if not configured:
                missing.append(env_name)
                continue
            path = Path(configured)
            _read_token(path)
            principals.append(Principal(name, role, scopes, path))
        if required and missing:
            raise RuntimeError(
                "Harness auth is required but token files are not configured: "
                + ", ".join(missing)
            )
        return cls(tuple(principals), enabled=required or bool(principals))

    @classmethod
    def disabled(cls) -> "TokenAuthorizer":
        return cls((), enabled=False)

    def authenticate(self, authorization: str | None, scope: str) -> Principal:
        if not self.enabled:
            return Principal("development", "development", frozenset({"*"}))
        if not authorization or not authorization.startswith("Bearer "):
            raise AuthenticationError("bearer token required")
        supplied = authorization.removeprefix("Bearer ").strip()
        for principal in self.principals:
            expected = _read_token(principal.token_file)
            if hmac.compare_digest(supplied, expected):
                if not principal.allows(scope):
                    raise AuthorizationError(
                        f"principal {principal.name} lacks scope {scope}"
                    )
                return principal
        raise AuthenticationError("bearer token invalid or revoked")


def required_scope(method: str, path: str) -> str | None:
    """Map the small API surface to explicit scopes; health remains public."""
    if path == "/health" or not path.startswith("/v1/"):
        return None
    if method == "POST" and path == "/v1/tasks":
        return "tasks:create"
    if method == "GET" and re.fullmatch(r"/v1/tasks/[0-9a-fA-F-]+", path):
        return "tasks:read"
    if method == "POST" and path.endswith("/claim"):
        return "tasks:claim"
    if method == "POST" and path.endswith("/heartbeat"):
        return "tasks:heartbeat"
    if method == "POST" and (
        path.endswith("/receipts") or path.endswith("/failures")
    ):
        return "tasks:receipt"
    if method == "GET" and (
        path.startswith("/v1/operations/") or path.startswith("/v1/traces/")
    ):
        return "operations:read"
    if path.startswith("/v1/maintenance/"):
        return "maintenance:write"
    if path.endswith("/approval-requests"):
        return "approvals:request"
    if "/v1/approvals/" in path and path.endswith("/decisions"):
        return "approvals:decide"
    return "authenticated"


def _read_token(path: Path | None) -> str:
    if path is None:
        raise RuntimeError("token file is not configured")
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"cannot read token file {path}") from exc
    if len(token) < 32:
        raise RuntimeError(f"token file {path} must contain at least 32 characters")
    return token
