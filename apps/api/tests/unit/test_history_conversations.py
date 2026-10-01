"""History grouped by conversation (chat): list, open, search, save and delete chats."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from fastapi import FastAPI

from tests.conftest import resources_of
from tests.unit.test_analytics_api import REGION_Q, demo_db, fixed_today
from tests.unit.test_semantic_api import setup_indexed

__all__ = ["demo_db", "fixed_today"]

TOP_Q = "Top 5 customers by revenue this year"


async def ask_in(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    cid: str,
    question: str,
    conversation: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"question": question, "connection_id": cid}
    if conversation is not None:
        body["conversation_id"] = conversation
    response = await client.post("/api/v1/analytics/query", json=body, headers=headers)
    result: dict[str, Any] = response.json()
    return result


async def chats(client: httpx.AsyncClient, headers: dict[str, str], query: str = "") -> Any:
    response = await client.get(f"/api/v1/history/conversations?{query}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def test_questions_of_one_chat_form_one_history_entry(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    admin, viewer, cid = await setup_indexed(app, client, demo_db)
    chat = str(uuid.uuid4())
    first = await ask_in(client, viewer, cid, REGION_Q, chat)
    second = await ask_in(client, viewer, cid, TOP_Q, chat)
    alone = await ask_in(client, viewer, cid, "total sales last month")  # a new chat
    await ask_in(client, admin, cid, "total sales last month", chat)  # another user's

    page = await chats(client, viewer)
    assert page["total"] == 2
    newest, grouped = page["items"]
    assert newest["conversation_id"] == alone["query_id"]  # a chat of its own
    assert newest["question_count"] == 1
    assert grouped["conversation_id"] == chat
    assert grouped["title"] == REGION_Q and grouped["last_question"] == TOP_Q
    assert grouped["question_count"] == 2 and grouped["saved"] is False
    assert grouped["connection_name"] == "Demo" and grouped["last_status"] == "succeeded"
    assert grouped["last_activity_at"] >= grouped["started_at"]

    detail = (await client.get(f"/api/v1/history/conversations/{chat}", headers=viewer)).json()
    assert [m["query_id"] for m in detail["messages"]] == [first["query_id"], second["query_id"]]
    assert detail["messages"][0]["response"]["query_id"] == first["query_id"]
    assert detail["title"] == REGION_Q

    # Per-question history is unchanged and now names each question's chat.
    items = (await client.get("/api/v1/history", headers=viewer)).json()["items"]
    assert {i["conversation_id"] for i in items} == {chat, alone["query_id"]}


async def test_follow_up_joins_its_parents_chat(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    parent = await ask_in(client, viewer, cid, REGION_Q)  # no id: a new chat
    follow = await client.post(
        "/api/v1/analytics/follow-up",
        json={"question": "only for EMEA", "parent_query_id": parent["query_id"]},
        headers=viewer,
    )
    assert follow.status_code in (200, 409, 422), follow.text
    page = await chats(client, viewer)
    assert page["total"] == 1
    assert page["items"][0]["conversation_id"] == parent["query_id"]
    assert page["items"][0]["question_count"] == 2


async def test_search_saved_filter_and_paging_work_per_chat(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    chat = str(uuid.uuid4())
    await ask_in(client, viewer, cid, "total sales last month", chat)
    await ask_in(client, viewer, cid, REGION_Q, chat)
    other = await ask_in(client, viewer, cid, TOP_Q)

    found = await chats(client, viewer, "q=region")  # matches the chat's 2nd question
    assert [c["conversation_id"] for c in found["items"]] == [chat]
    assert found["items"][0]["title"] == "total sales last month"
    assert (await chats(client, viewer, "q=nothing-like-this"))["total"] == 0

    saved = await client.patch(
        f"/api/v1/history/conversations/{chat}", json={"saved": True}, headers=viewer
    )
    assert saved.status_code == 200 and saved.json()["saved"] is True
    assert [c["conversation_id"] for c in (await chats(client, viewer, "saved=true"))["items"]] == [
        chat
    ]
    assert [
        c["conversation_id"] for c in (await chats(client, viewer, "saved=false"))["items"]
    ] == [other["query_id"]]
    questions = (await client.get("/api/v1/history?saved=true", headers=viewer)).json()
    assert questions["total"] == 2  # saving a chat saves each of its questions

    first_page = await chats(client, viewer, "limit=1")
    second_page = await chats(client, viewer, "limit=1&offset=1")
    assert first_page["total"] == 2 and len(first_page["items"]) == 1
    assert first_page["items"][0]["conversation_id"] != second_page["items"][0]["conversation_id"]


async def test_deleting_a_chat_deletes_all_its_questions(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    admin, viewer, cid = await setup_indexed(app, client, demo_db)
    chat = str(uuid.uuid4())
    first = await ask_in(client, viewer, cid, REGION_Q, chat)
    await ask_in(client, viewer, cid, TOP_Q, chat)

    # Chats are personal: another user sees 404 and cannot delete it.
    assert (
        await client.get(f"/api/v1/history/conversations/{chat}", headers=admin)
    ).status_code == 404
    assert (
        await client.delete(f"/api/v1/history/conversations/{chat}", headers=admin)
    ).status_code == 404

    deleted = await client.delete(f"/api/v1/history/conversations/{chat}", headers=viewer)
    assert deleted.status_code == 204
    assert (await chats(client, viewer))["total"] == 0
    assert (await client.get("/api/v1/history", headers=viewer)).json()["total"] == 0
    assert (
        await client.get(f"/api/v1/history/{first['query_id']}", headers=viewer)
    ).status_code == 404
    assert (
        await client.get(f"/api/v1/history/conversations/{chat}", headers=viewer)
    ).status_code == 404
    cached = await resources_of(app).redis.keys("*")
    assert not any(first["query_id"] in str(k) for k in cached)  # answers dropped too


async def test_unknown_or_malformed_chat_ids(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, _ = await setup_indexed(app, client, demo_db)
    missing = await client.get(f"/api/v1/history/conversations/{uuid.uuid4()}", headers=viewer)
    assert missing.status_code == 404
    bad = await client.get("/api/v1/history/conversations/not-a-uuid", headers=viewer)
    assert bad.status_code == 422
    body = await client.post(
        "/api/v1/analytics/query",
        json={"question": REGION_Q, "connection_id": str(uuid.uuid4()), "conversation_id": "x"},
        headers=viewer,
    )
    assert body.status_code == 422
