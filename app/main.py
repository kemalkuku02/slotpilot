import asyncio
import hmac
import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime
from html import escape

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.agent.engine import Agent
from app.booking.service import book, find_slots
from app.config.loader import get_config
from app.db.models import Base, Booking, Client, Conversation, ConversationStatus, Message
from app.db.session import SessionLocal, engine


@asynccontextmanager
async def lifespan(_: FastAPI):
    if engine.dialect.name == "sqlite":
        Base.metadata.create_all(engine)
    yield


app = FastAPI(title="SlotPilot", version="0.1.0", lifespan=lifespan)
security = HTTPBasic()
agent = Agent()


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
    after: str | None = None


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
    from datetime import time
    try:
        after = time.fromisoformat(query.after) if query.after else None
        return find_slots(db, get_config(), query.service_id, query.start_date, query.end_date, after=after)
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
    return HTMLResponse("""<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>SlotPilot — диалоги</title>
<style>body{font:15px system-ui;background:#f3f5f7;color:#18202a;margin:0}header{padding:18px 5vw;background:#fff;border-bottom:1px solid #dce2e7}main{display:grid;grid-template-columns:minmax(250px,34%) 1fr;gap:18px;max-width:1200px;margin:22px auto;padding:0 16px}.card{background:white;border:1px solid #dce2e7;border-radius:12px;padding:16px}button{border:0;border-radius:7px;background:#234c68;color:#fff;padding:9px 13px;cursor:pointer;margin:3px}.urgent{color:#a52020;font-weight:700}#list button{display:block;width:100%;text-align:left;background:#f4f6f7;color:#18202a;padding:12px}#messages{height:58vh;overflow:auto;border-block:1px solid #eee;padding:8px}.msg{padding:9px;margin:6px;border-radius:8px;background:#edf2f5}.client{margin-left:14%;background:#e8f2ff}input{padding:10px;width:75%;border:1px solid #ccd3d9;border-radius:7px}@media(max-width:700px){main{grid-template-columns:1fr}}</style>
<header><b>SlotPilot</b> <span>Администрирование диалогов</span></header><main><section class="card"><h2>Диалоги</h2><div id="list"></div></section><section class="card"><div id="detail"><h2>Выберите диалог</h2></div></section></main>
<script>let current=null;async function load(){let r=await fetch('/admin/api/conversations');if(r.ok){let a=await r.json();document.querySelector('#list').innerHTML=a.map(x=>`<button onclick="openChat(${x.id})"><span class="${x.status==='URGENT_HUMAN'?'urgent':''}">${x.status}</span><br>${esc(x.client_name||'Клиент '+x.client_id)} · ${esc(x.channel)}</button>`).join('')}}async function openChat(id){current=id;let r=await fetch('/admin/api/conversations/'+id);let x=await r.json();document.querySelector('#detail').innerHTML=`<h2>${esc(x.client_name||'Клиент')} · <span class="${x.status==='URGENT_HUMAN'?'urgent':''}">${x.status}</span></h2><p>${esc(x.phone||'')}</p><button onclick="action('claim')">Перехватить</button><button onclick="action('return')">Вернуть боту</button><div id="messages">${x.messages.map(m=>`<div class="msg ${m.role==='client'?'client':''}"><small>${esc(m.role)}</small><br>${esc(m.text)}</div>`).join('')}</div><form onsubmit="send(event)"><input id="reply" placeholder="Ответ клиенту"><button>Отправить</button></form>`}function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}async function action(a){await fetch(`/admin/api/conversations/${current}/${a}`,{method:'POST'});await load();await openChat(current)}async function send(e){e.preventDefault();let text=document.querySelector('#reply').value;await fetch(`/admin/api/conversations/${current}/reply`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})});await openChat(current)}load();new EventSource('/admin/events').onmessage=()=>{load();if(current)openChat(current)};</script></html>""")


@app.get("/admin/api/conversations")
def admin_conversations(_: str = Depends(admin_auth), db: Session = Depends(get_db)):
    rows = db.scalars(select(Conversation).order_by(Conversation.updated_at.desc()).limit(200)).all()
    return [{"id": c.id, "client_id": c.client_id, "client_name": c.client.name, "status": c.status, "channel": c.channel, "updated_at": c.updated_at} for c in rows]


@app.get("/admin/api/conversations/{conversation_id}")
def admin_conversation(conversation_id: int, _: str = Depends(admin_auth), db: Session = Depends(get_db)):
    c = db.get(Conversation, conversation_id)
    if c is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return {"id": c.id, "client_id": c.client_id, "client_name": c.client.name, "phone": c.client.phone, "status": c.status,
            "messages": [{"id": m.id, "role": m.role, "text": m.text, "created_at": m.created_at} for m in sorted(c.messages, key=lambda m: m.id)]}


@app.post("/admin/api/conversations/{conversation_id}/claim")
def claim_conversation(conversation_id: int, _: str = Depends(admin_auth), db: Session = Depends(get_db)):
    c = db.get(Conversation, conversation_id)
    if c is None: raise HTTPException(status_code=404, detail="conversation not found")
    c.status = ConversationStatus.HUMAN.value
    db.commit()
    return {"status": c.status}


@app.post("/admin/api/conversations/{conversation_id}/return")
def return_conversation(conversation_id: int, _: str = Depends(admin_auth), db: Session = Depends(get_db)):
    c = db.get(Conversation, conversation_id)
    if c is None: raise HTTPException(status_code=404, detail="conversation not found")
    c.status = ConversationStatus.BOT.value
    db.commit()
    return {"status": c.status}


class AdminReply(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@app.post("/admin/api/conversations/{conversation_id}/reply")
def admin_reply(conversation_id: int, payload: AdminReply, _: str = Depends(admin_auth), db: Session = Depends(get_db)):
    c = db.get(Conversation, conversation_id)
    if c is None: raise HTTPException(status_code=404, detail="conversation not found")
    if c.status == ConversationStatus.BOT.value:
        raise HTTPException(status_code=409, detail="Claim the conversation before replying")
    db.add(Message(conversation_id=c.id, role="admin", text=payload.text))
    db.commit()
    if c.channel == "telegram":
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not token or not c.client.telegram_id:
            raise HTTPException(status_code=503, detail="Telegram is not configured")
        import httpx
        try:
            sent = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": c.client.telegram_id, "text": payload.text}, timeout=15)
            sent.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="Telegram send failed") from exc
    return {"ok": True}


@app.get("/admin/events")
async def admin_events(_: str = Depends(admin_auth)):
    async def events():
        yield "event: ready\ndata: {}\n\n"
        while True:
            await asyncio.sleep(3)
            yield "data: {}\n\n"
    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
