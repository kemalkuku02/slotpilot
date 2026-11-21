import pytest
from fastapi import HTTPException
from fastapi.security import HTTPBasicCredentials
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.main as main
from app.db.models import Base, Client, Conversation, Message


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
