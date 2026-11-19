from app.agent.engine import Agent, guard_price_claims, is_explicit_confirmation
from app.agent.provider import FakeLLM, tool_call
from app.db.models import Booking, Client, Conversation, Message, PendingBooking
from app.booking.service import find_slots
from datetime import date, datetime, time, timedelta, timezone
from app.safety.filters import PIIMask, check_red_flags, normalize


def test_red_flags_normalize_case_and_yo(config):
    assert check_red_flags("У меня ОСТРАЯ   БОЛЬ", config.red_flags.keywords)
    assert normalize("Ёж") == "еж"


def test_personal_data_mask_and_restore():
    mask = PIIMask()
    source = "Позвоните +7 999 123-45-67, email user@example.com, я Алексей Иванов"
    safe = mask.mask(source)
    assert "+7 999 123-45-67" not in safe
    assert "user@example.com" not in safe
    assert "Алексей Иванов" not in safe
    assert mask.unmask(safe) == source


def test_price_guard_keeps_configured_price_and_replaces_wrong_claim(config):
    assert guard_price_claims("Стоимость 1 000 рублей.", config) == "Стоимость 1 000 рублей."
    safe = guard_price_claims("Услуга стоит 9 999 рублей", config)
    assert "9 999" not in safe
    assert "1000" in safe


def test_booking_requires_confirmation_after_fixed_quote(db, config):
    client = Client(telegram_id="confirm-user")
    db.add(client)
    db.flush()
    conversation = Conversation(client_id=client.id, channel="telegram")
    db.add(conversation)
    db.flush()
    day = date.today() + timedelta(days=2)
    slot = find_slots(db, config, "service", day, day)[0]
    db.add(Message(conversation_id=conversation.id, role="client", text="Хочу записаться"))
    db.commit()
    fake = FakeLLM([tool_call("prepare_booking", {
        "service_id": "service", "specialist_id": "one", "start_at": slot["start_at"],
        "name": "Алексей Иванов", "phone": "+79990000000",
    })])
    quote = Agent(fake).reply(db, client, conversation, "Хочу записаться", config)
    assert quote.startswith("Проверьте детали:")
    assert db.query(Booking).count() == 0
    assert db.query(PendingBooking).count() == 1
    db.add(Message(conversation_id=conversation.id, role="client", text="Подтверждаю"))
    db.commit()
    Agent(FakeLLM([])).reply(db, client, conversation, "Подтверждаю", config)
    assert db.query(Booking).count() == 1
    assert db.query(PendingBooking).one().status == "booked"


def test_plain_yes_is_not_explicit_booking_confirmation():
    assert not is_explicit_confirmation("да")
    assert not is_explicit_confirmation("да, но можно позже?")
    assert is_explicit_confirmation("Да, подтверждаю!")


def test_urgent_message_escalates_before_llm(db, config):
    client = Client(telegram_id="safety-user")
    db.add(client)
    db.flush()
    conversation = Conversation(client_id=client.id, channel="telegram")
    db.add(conversation)
    db.flush()
    db.add(Message(conversation_id=conversation.id, role="client", text="У меня аллергия"))
    db.commit()
    reply = Agent(provider=None).reply(db, client, conversation, "У меня аллергия", config)
    assert conversation.status == "URGENT_HUMAN"
    assert reply == config.red_flags.reply
