"""Authentication request/response models (contract shared with the extension)."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import Field, SecretStr

from app.core.security import MAX_PASSWORD_LENGTH
from app.schemas.common import Email, ResponseModel, StrictModel, TenantSlug


class LoginRequest(StrictModel):
    tenant: TenantSlug
    email: Email
    password: SecretStr = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class RefreshRequest(StrictModel):
    refresh_token: SecretStr = Field(min_length=20, max_length=200)


class LogoutRequest(StrictModel):
    refresh_token: SecretStr = Field(min_length=20, max_length=200)


class TenantOut(ResponseModel):
    id: uuid.UUID
    slug: str
    name: str


class MeResponse(ResponseModel):
    id: uuid.UUID
    email: str
    display_name: str
    tenant: TenantOut
    roles: list[str]
    permissions: list[str]


class TokenResponse(ResponseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - OAuth2 token type, not a secret
    expires_in: int
    refresh_token: str
    refresh_expires_in: int
    user: MeResponse
