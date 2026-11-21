import httpx
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPBasicCredentials
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.main as main
from app.db.models import Base, Client, Conversation, ConversationStatus, Message


def test_health_chat_identity_and_admin_auth(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    main.agent = main.Agent(provider=None)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setenv("ADMIN_USERNAME", "test-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "a-long-test-password")
    try:
        with Session(engine, expire_on_commit=False) as db:
            assert main.health(db) == {"status": "ok", "database": "ok"}
            first = main.chat(main.ChatRequest(user_id="same-user", text="Здравствуйте"), db)
            second = main.chat(main.ChatRequest(user_id="same-user", text="Покажите услуги"), db)
            assert first["conversation_id"] == second["conversation_id"]
            assert "LLM" in second["reply"]
        with pytest.raises(HTTPException) as error:
            main.admin_auth(HTTPBasicCredentials(username="bad", password="bad"))
        assert error.value.status_code == 401
        assert main.admin_auth(HTTPBasicCredentials(username="test-admin", password="a-long-test-password")) == "test-admin"
    finally:
        engine.dispose()


def test_home_redirects_to_admin():
    response = main.home()
    assert response.status_code == 307
    assert response.headers["location"] == "/admin"


def test_admin_page_serves_inbox_template():
    response = main.admin_page("admin")
    assert response.status_code == 200
    assert "Панель администратора" in response.body.decode("utf-8")
    assert "status-filter" in response.body.decode("utf-8")


def test_admin_conversation_list_includes_latest_message(db):
    client = Client(telegram_id="admin-preview")
    db.add(client)
    db.flush()
    conversation = Conversation(client_id=client.id, channel="telegram")
    db.add(conversation)
    db.flush()
    db.add_all([
        Message(conversation_id=conversation.id, role="client", text="Первое сообщение"),
        Message(conversation_id=conversation.id, role="bot", text="Последний ответ"),
    ])
    db.commit()

    result = main.admin_conversations("admin", db)

    assert len(result) == 1
    assert result[0]["last_message"] == "Последний ответ"


def test_admin_telegram_reply_requires_claim_and_persists_after_delivery(db, monkeypatch):
    client = Client(telegram_id="telegram-chat-42")
    db.add(client)
    db.flush()
    conversation = Conversation(client_id=client.id, channel="telegram", status=ConversationStatus.BOT.value)
    db.add(conversation)
    db.commit()

    with pytest.raises(HTTPException) as not_claimed:
        main.admin_reply(conversation.id, main.AdminReply(text="Здравствуйте"), "admin", db)
    assert not_claimed.value.status_code == 409
    assert db.query(Message).filter_by(conversation_id=conversation.id, role="admin").count() == 0

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-bot-token")
    main.claim_conversation(conversation.id, "admin", db)
    sent = {}

    def send_message(url, *, json, timeout):
        sent.update(url=url, json=json, timeout=timeout)
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", send_message)
    result = main.admin_reply(conversation.id, main.AdminReply(text="Здравствуйте"), "admin", db)

    assert result == {"ok": True}
    assert sent["url"].endswith("/sendMessage")
    assert sent["json"] == {"chat_id": "telegram-chat-42", "text": "Здравствуйте"}
    assert db.query(Message).filter_by(conversation_id=conversation.id, role="admin").one().text == "Здравствуйте"


def test_admin_telegram_failure_does_not_save_unsent_reply(db, monkeypatch):
    client = Client(telegram_id="telegram-chat-failure")
    db.add(client)
    db.flush()
    conversation = Conversation(client_id=client.id, channel="telegram", status=ConversationStatus.HUMAN.value)
    db.add(conversation)
    db.commit()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-bot-token")

    def fail_to_send(url, **kwargs):
        request = httpx.Request("POST", url)
        raise httpx.ConnectError("telegram unavailable", request=request)

    monkeypatch.setattr(httpx, "post", fail_to_send)

    with pytest.raises(HTTPException) as failure:
        main.admin_reply(conversation.id, main.AdminReply(text="Ответ"), "admin", db)

    assert failure.value.status_code == 502
    assert db.query(Message).filter_by(conversation_id=conversation.id, role="admin").count() == 0


def test_closed_conversation_cannot_be_claimed_or_returned(db):
    client = Client(telegram_id="closed-conversation")
    db.add(client)
    db.flush()
    conversation = Conversation(client_id=client.id, status=ConversationStatus.CLOSED.value)
    db.add(conversation)
    db.commit()

    with pytest.raises(HTTPException) as claim_error:
        main.claim_conversation(conversation.id, "admin", db)
    with pytest.raises(HTTPException) as return_error:
        main.return_conversation(conversation.id, "admin", db)

    assert claim_error.value.status_code == 409
    assert return_error.value.status_code == 409
    db.refresh(conversation)
    assert conversation.status == ConversationStatus.CLOSED.value
