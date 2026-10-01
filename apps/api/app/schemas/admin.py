"""Tenant user administration models."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field, SecretStr

from app.core.permissions import Role
from app.core.security import MAX_PASSWORD_LENGTH
from app.schemas.common import DisplayName, Email, ResponseModel, StrictModel


class UserCreate(StrictModel):
    email: Email
    display_name: DisplayName
    password: SecretStr = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    roles: list[Role] = Field(min_length=1, max_length=5)


class UserUpdate(StrictModel):
    display_name: DisplayName | None = None
    is_active: bool | None = None
    roles: list[Role] | None = Field(default=None, min_length=1, max_length=5)


class UserOut(ResponseModel):
    id: uuid.UUID
    email: str
    display_name: str
    is_active: bool
    roles: list[str]
    last_login_at: datetime | None
    locked: bool
    created_at: datetime


class UserList(ResponseModel):
    items: list[UserOut]
    total: int
