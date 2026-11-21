import asyncio
import hmac
import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, joinedload

from app.agent.engine import Agent
from app.booking.service import book, find_slots
from app.config.loader import get_config, save_config
from app.config.schema import BusinessConfig, Specialist
from app.db.models import Base, Booking, Client, Conversation, ConversationStatus, Message, PendingBooking, utcnow
from app.db.session import SessionLocal, engine


@asynccontextmanager
async def lifespan(_: FastAPI):
    if engine.dialect.name == "sqlite":
        Base.metadata.create_all(engine)
    yield


app = FastAPI(title="SlotPilot", version="0.1.0", lifespan=lifespan)
security = HTTPBasic()
agent = Agent()


@app.get("/", include_in_schema=False)
def home():
    return RedirectResponse(url="/admin", status_code=307)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def admin_auth(credentials: HTTPBasicCredentials = Depends(security)):
    username = os.getenv("ADMIN_USERNAME", "admin")
    password = os.getenv("ADMIN_PASSWORD")
    if not password:
        raise HTTPException(status_code=503, detail="Set ADMIN_PASSWORD to enable the admin panel")
    if not (hmac.compare_digest(credentials.username, username) and hmac.compare_digest(credentials.password, password)):
        raise HTTPException(status_code=401, detail="Invalid credentials", headers={"WWW-Authenticate": "Basic"})
    return credentials.username


class SlotQuery(BaseModel):
    service_id: str
    start_date: date
    end_date: date
    after: time | None = None
    before: time | None = None
    limit: int = Field(default=20, ge=1, le=1000)


class BookingRequest(BaseModel):
    client_id: int
    service_id: str
    specialist_id: str
    start_at: datetime
    idempotency_key: str = Field(min_length=8, max_length=128)


class ChatRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=4000)


def _get_conversation(db: Session, channel: str, user_id: str):
    stored_user_id = user_id if channel == "telegram" else f"web:{user_id}"
    client = db.scalar(select(Client).where(Client.telegram_id == stored_user_id))
    if client is None:
        client = Client(telegram_id=stored_user_id)
        db.add(client)
        db.commit()
        db.refresh(client)
    conversation = db.scalar(select(Conversation).where(
        Conversation.client_id == client.id,
        Conversation.status.in_([ConversationStatus.BOT.value, ConversationStatus.HUMAN.value, ConversationStatus.URGENT_HUMAN.value]),
    ).order_by(Conversation.id.desc()))
    if conversation is None or conversation.status == ConversationStatus.CLOSED.value:
        conversation = Conversation(client_id=client.id, channel=channel)
        db.add(conversation)
        db.commit()
        db.refresh(conversation)
    return client, conversation


def _receive(db: Session, channel: str, user_id: str, message_text: str):
    client, conversation = _get_conversation(db, channel, user_id)
    db.add(Message(conversation_id=conversation.id, role="client", text=message_text))
    conversation.updated_at = utcnow()
    db.commit()
    answer = agent.reply(db, client, conversation, message_text, get_config())
    return conversation.id, answer


@app.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {"status": "ok", "database": "ok"}


@app.get("/api/services")
def services():
    config = get_config()
    return [{"id": s.id, "name": s.name, "price": s.price, "duration_min": s.duration_min} for s in config.services]


@app.post("/api/slots")
def slots(query: SlotQuery, db: Session = Depends(get_db)):
    try:
        return find_slots(db, get_config(), query.service_id, query.start_date, query.end_date,
                          after=query.after, before=query.before, limit=query.limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/bookings")
def create_booking(payload: BookingRequest, _: str = Depends(admin_auth), db: Session = Depends(get_db)):
    config = get_config()
    try:
        booking = book(db, config=config, client_id=payload.client_id, service_id=payload.service_id,
                       specialist_id=payload.specialist_id, start_at=payload.start_at,
                       idempotency_key=payload.idempotency_key)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"id": booking.id, "status": booking.status, "start_at": booking.start_at, "end_at": booking.end_at}


@app.post("/api/chat")
def chat(payload: ChatRequest, db: Session = Depends(get_db)):
    conversation_id, answer = _receive(db, "web", payload.user_id, payload.text)
    return {"conversation_id": conversation_id, "reply": answer}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None), db: Session = Depends(get_db)):
    expected = os.getenv("TELEGRAM_WEBHOOK_SECRET")
    if not expected or not hmac.compare_digest(x_telegram_bot_api_secret_token or "", expected):
        raise HTTPException(status_code=403, detail="invalid webhook secret")
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise HTTPException(status_code=503, detail="Telegram bot is not configured")
    update = await request.json()
    message = update.get("message") or update.get("edited_message")
    if not message or not message.get("text"):
        return {"ok": True}
    chat_id = str(message["chat"]["id"])
    user = message.get("from", {})
    _, answer = _receive(db, "telegram", str(user.get("id", chat_id)), message["text"])
    import httpx
    try:
        result = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": answer}, timeout=15)
        result.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Telegram send failed") from exc
    return {"ok": True}


@app.get("/admin", response_class=HTMLResponse)
def admin_page(_: str = Depends(admin_auth)):
    template = Path(__file__).parent / "templates" / "admin.html"
    return HTMLResponse(template.read_text(encoding="utf-8"))


@app.get("/admin/team", response_class=HTMLResponse)
def admin_team_page(_: str = Depends(admin_auth)):
    template = Path(__file__).parent / "templates" / "team.html"
    return HTMLResponse(template.read_text(encoding="utf-8"))


@app.get("/admin/api/team")
def admin_team(_: str = Depends(admin_auth)):
    config = get_config()
    return {
        "business_name": config.business_name,
        "timezone": config.timezone,
        "weekdays": ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"],
        "specialists": [{
            "id": specialist.id,
            "name": specialist.name,
            "schedule": {
                str(day): [{"start": interval.start.strftime("%H:%M"), "end": interval.end.strftime("%H:%M")}
                           for interval in intervals]
                for day, intervals in specialist.schedule.items()
            },
        } for specialist in config.specialists],
        "services": [{"id": service.id, "name": service.name, "price": service.price,
                      "duration_min": service.duration_min,
                      "specialist_ids": service.specialist_ids} for service in config.services],
    }


class AdminTeamUpdate(BaseModel):
    specialists: list[Specialist]
    service_specialists: dict[str, list[str]]


@app.put("/admin/api/team")
def update_admin_team(payload: AdminTeamUpdate, _: str = Depends(admin_auth)):
    current = get_config()
    data = current.model_dump(mode="python")
    service_ids = {service.id for service in current.services}
    if set(payload.service_specialists) != service_ids:
        raise HTTPException(status_code=422, detail="Набор услуг в запросе не совпадает с конфигурацией")
    data["specialists"] = [specialist.model_dump(mode="python") for specialist in payload.specialists]
    for service in data["services"]:
        service["specialist_ids"] = payload.service_specialists[service["id"]]
    try:
        updated = BusinessConfig.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        save_config(updated)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Не удалось сохранить локальную конфигурацию") from exc
    return {"ok": True, "storage": "instance/configs"}


@app.get("/admin/api/conversations")
def admin_conversations(_: str = Depends(admin_auth), db: Session = Depends(get_db)):
    last_message = select(Message.text).where(Message.conversation_id == Conversation.id).order_by(Message.id.desc()).limit(1).scalar_subquery()
    rows = db.execute(select(Conversation, last_message.label("last_message"))
                      .options(joinedload(Conversation.client))
                      .order_by(Conversation.updated_at.desc()).limit(200)).all()
    return [{"id": c.id, "client_id": c.client_id, "client_name": c.client.name, "status": c.status,
             "channel": c.channel, "updated_at": c.updated_at,
             "last_message": preview[:180] if preview else None}
            for c, preview in rows]


@app.get("/admin/api/conversations/{conversation_id}")
def admin_conversation(conversation_id: int, _: str = Depends(admin_auth), db: Session = Depends(get_db)):
    c = db.get(Conversation, conversation_id)
    if c is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    config = get_config()
    booking_details = []
    for booking in sorted(c.client.bookings, key=lambda b: b.start_at, reverse=True):
        service = next((s for s in config.services if s.id == booking.service_id), None)
        booking_details.append({"id": booking.id, "status": booking.status, "service_name": service.name if service else booking.service_id,
                                "price": service.price if service else None, "start_at": booking.start_at})
    return {"id": c.id, "client_id": c.client_id, "client_name": c.client.name, "phone": c.client.phone, "status": c.status,
            "bookings": booking_details,
            "messages": [{"id": m.id, "role": m.role, "text": m.text, "created_at": m.created_at} for m in sorted(c.messages, key=lambda m: m.id)]}


@app.post("/admin/api/conversations/{conversation_id}/claim")
def claim_conversation(conversation_id: int, _: str = Depends(admin_auth), db: Session = Depends(get_db)):
    c = db.get(Conversation, conversation_id)
    if c is None: raise HTTPException(status_code=404, detail="conversation not found")
    if c.status == ConversationStatus.CLOSED.value:
        raise HTTPException(status_code=409, detail="Закрытый диалог нельзя перехватить")
    c.status = ConversationStatus.HUMAN.value
    db.query(PendingBooking).filter_by(conversation_id=c.id, status="pending").update({"status": "cancelled"})
    db.commit()
    return {"status": c.status}


@app.post("/admin/api/conversations/{conversation_id}/return")
def return_conversation(conversation_id: int, _: str = Depends(admin_auth), db: Session = Depends(get_db)):
    c = db.get(Conversation, conversation_id)
    if c is None: raise HTTPException(status_code=404, detail="conversation not found")
    if c.status == ConversationStatus.CLOSED.value:
        raise HTTPException(status_code=409, detail="Закрытый диалог нельзя вернуть боту")
    c.status = ConversationStatus.BOT.value
    db.commit()
    return {"status": c.status}


@app.post("/admin/api/conversations/{conversation_id}/close")
def close_conversation(conversation_id: int, _: str = Depends(admin_auth), db: Session = Depends(get_db)):
    c = db.get(Conversation, conversation_id)
    if c is None: raise HTTPException(status_code=404, detail="conversation not found")
    c.status = ConversationStatus.CLOSED.value
    db.commit()
    return {"status": c.status}


class AdminReply(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@app.post("/admin/api/conversations/{conversation_id}/reply")
def admin_reply(conversation_id: int, payload: AdminReply, _: str = Depends(admin_auth), db: Session = Depends(get_db)):
    c = db.get(Conversation, conversation_id)
    if c is None: raise HTTPException(status_code=404, detail="conversation not found")
    if c.status != ConversationStatus.HUMAN.value:
        raise HTTPException(status_code=409, detail="Сначала перехватите диалог")
    if c.channel == "telegram":
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not token or not c.client.telegram_id:
            raise HTTPException(status_code=503, detail="Telegram is not configured")
        import httpx
        try:
            sent = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": c.client.telegram_id, "text": payload.text}, timeout=15)
            sent.raise_for_status()
            result = sent.json()
        except (httpx.HTTPError, ValueError) as exc:
            db.rollback()
            raise HTTPException(status_code=502, detail="Telegram send failed") from exc
        if not isinstance(result, dict) or result.get("ok") is not True:
            db.rollback()
            raise HTTPException(status_code=502, detail="Telegram send failed")
    db.add(Message(conversation_id=c.id, role="admin", text=payload.text))
    c.updated_at = utcnow()
    db.commit()
    return {"ok": True}


@app.get("/admin/events")
async def admin_events(_: str = Depends(admin_auth)):
    async def events():
        yield "event: ready\ndata: {}\n\n"
        previous = None
        while True:
            with SessionLocal() as db:
                updated_at = db.scalar(select(func.max(Conversation.updated_at)))
                last_message_id = db.scalar(select(func.max(Message.id)))
            marker = {
                "updated_at": updated_at.isoformat() if updated_at else None,
                "last_message_id": last_message_id,
            }
            if marker != previous:
                yield f"data: {json.dumps(marker)}\n\n"
                previous = marker
            else:
                yield ": keep-alive\n\n"
            await asyncio.sleep(3)
    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
