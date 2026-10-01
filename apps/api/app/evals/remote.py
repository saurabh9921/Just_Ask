"""Evaluate a running deployment over HTTP.

Credentials come from the environment only (never command-line arguments):
``ERP_EVAL_PASSWORD`` for the analyst account and, optionally,
``ERP_EVAL_UNGRANTED_PASSWORD`` for an account whose role has no grant on the
connection (the ``unauthorized`` connection case is skipped without it).
Result correctness needs the in-process gold executor, so it is not scored here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from app.evals.runner import UserName


class RemoteLoginError(RuntimeError):
    pass


@dataclass
class RemoteTarget:
    client: httpx.AsyncClient
    connection_id: str
    headers: dict[str, dict[str, str]] = field(default_factory=dict)

    def has_user(self, user: UserName) -> bool:
        return user in self.headers

    async def login(self, user: UserName, tenant: str, email: str, password: str) -> None:
        response = await self.client.post(
            "/api/v1/auth/login", json={"tenant": tenant, "email": email, "password": password}
        )
        if response.status_code != 200:
            raise RemoteLoginError(f"login for the {user} account failed ({response.status_code})")
        self.headers[user] = {"Authorization": f"Bearer {response.json()['access_token']}"}

    async def ask(self, question: str, user: UserName) -> tuple[int, dict[str, Any]]:
        response = await self.client.post(
            "/api/v1/analytics/query",
            json={"question": question, "connection_id": self.connection_id},
            headers=self.headers[user],
        )
        try:
            body = response.json()
        except ValueError:
            body = {}
        return response.status_code, body if isinstance(body, dict) else {}
