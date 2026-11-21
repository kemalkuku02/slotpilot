import json

from app.agent.engine import Agent, guard_price_claims, is_explicit_confirmation
from app.agent.provider import FakeLLM, tool_call
from app.db.models import Booking, Client, Conversation, Message, PendingBooking
from app.booking.service import book, find_slots
from datetime import date, datetime, time, timedelta, timezone
from app.safety.filters import PIIMask, check_red_flags, needs_secondary_triage, normalize


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


def test_ambiguous_symptom_uses_secondary_triage_and_escalates(db, config):
    client = Client(telegram_id="symptom-user")
    db.add(client)
    db.flush()
    conversation = Conversation(client_id=client.id, channel="telegram")
    db.add(conversation)
    db.flush()
    db.add(Message(conversation_id=conversation.id, role="client", text="У меня ноет зуб и припухла десна"))
    db.commit()
    fake = FakeLLM([{"role": "assistant", "content": '{"urgent": true}'}])

    reply = Agent(fake).reply(db, client, conversation, "У меня ноет зуб и припухла десна", config)

    assert conversation.status == "URGENT_HUMAN"
    assert reply == config.red_flags.reply
    assert len(fake.requests) == 1
    assert fake.requests[0][0]["role"] == "system"


def test_ambiguous_nonurgent_symptom_continues_to_regular_agent(db, config):
    client = Client(telegram_id="mild-symptom-user")
    db.add(client)
    db.flush()
    conversation = Conversation(client_id=client.id, channel="telegram")
    db.add(conversation)
    db.flush()
    db.add(Message(conversation_id=conversation.id, role="client", text="Зуб иногда ноет после холодной воды"))
    db.commit()
    fake = FakeLLM([
        {"role": "assistant", "content": '{"urgent": false}'},
        {"role": "assistant", "content": "Могу помочь подобрать время для записи."},
    ])

    reply = Agent(fake).reply(db, client, conversation, "Зуб иногда ноет после холодной воды", config)

    assert conversation.status == "BOT"
    assert reply == "Могу помочь подобрать время для записи."
    assert len(fake.requests) == 2


def test_secondary_triage_fails_closed_on_invalid_classifier_response(db, config):
    client = Client(telegram_id="uncertain-symptom-user")
    db.add(client)
    db.flush()
    conversation = Conversation(client_id=client.id, channel="telegram")
    db.add(conversation)
    db.flush()
    db.add(Message(conversation_id=conversation.id, role="client", text="У меня кровь и болит десна"))
    db.commit()
    fake = FakeLLM([{"role": "assistant", "content": "Не могу определить."}])

    Agent(fake).reply(db, client, conversation, "У меня кровь и болит десна", config)

    assert conversation.status == "URGENT_HUMAN"


def test_secondary_triage_only_runs_for_symptom_candidates(db, config):
    client = Client(telegram_id="routine-user")
    db.add(client)
    db.flush()
    conversation = Conversation(client_id=client.id, channel="telegram")
    db.add(conversation)
    db.flush()
    db.add(Message(conversation_id=conversation.id, role="client", text="Запишите меня на чистку"))
    db.commit()
    fake = FakeLLM([{"role": "assistant", "content": "Подберу время."}])

    Agent(fake).reply(db, client, conversation, "Запишите меня на чистку", config)

    assert len(fake.requests) == 1
    assert needs_secondary_triage("Запишите меня на чистку") is False


def test_customer_can_only_see_their_own_future_bookings(db, config):
    client = Client(telegram_id="booking-owner")
    other = Client(telegram_id="other-booking-owner")
    db.add_all([client, other])
    db.flush()
    conversation = Conversation(client_id=client.id, channel="telegram")
    db.add(conversation)
    db.flush()
    day = date.today() + timedelta(days=2)
    slots = find_slots(db, config, "service", day, day, limit=2)
    own_booking = book(db, config=config, client_id=client.id, service_id="service", specialist_id="one",
                       start_at=datetime.fromisoformat(slots[0]["start_at"]), idempotency_key="owner-booking")
    other_slot = find_slots(db, config, "service", day, day, limit=1)[0]
    book(db, config=config, client_id=other.id, service_id="service", specialist_id="one",
         start_at=datetime.fromisoformat(other_slot["start_at"]), idempotency_key="other-booking")
    db.add(Message(conversation_id=conversation.id, role="client", text="Покажи мои записи"))
    db.commit()
    fake = FakeLLM([tool_call("get_my_bookings", {}), {"role": "assistant", "content": "Вот ваши записи."}])

    Agent(fake).reply(db, client, conversation, "Покажи мои записи", config)

    tool_result = next(message for message in fake.requests[1] if message.get("role") == "tool")
    bookings = json.loads(tool_result["content"])
    assert [item["booking_id"] for item in bookings] == [own_booking.id]


def test_confirmation_handles_slot_taken_after_offer(db, config):
    client = Client(telegram_id="pending-owner")
    competitor = Client(telegram_id="pending-competitor")
    db.add_all([client, competitor])
    db.flush()
    conversation = Conversation(client_id=client.id, channel="telegram")
    db.add(conversation)
    db.flush()
    day = date.today() + timedelta(days=2)
    slot = find_slots(db, config, "service", day, day)[0]
    db.add(Message(conversation_id=conversation.id, role="client", text="Хочу записаться"))
    db.commit()
    fake = FakeLLM([tool_call("prepare_booking", {
        "service_id": "service", "specialist_id": slot["specialist_id"], "start_at": slot["start_at"],
        "name": "Алексей Иванов", "phone": "+79990000000",
    })])
    Agent(fake).reply(db, client, conversation, "Хочу записаться", config)
    pending = db.query(PendingBooking).one()
    book(db, config=config, client_id=competitor.id, service_id="service", specialist_id=slot["specialist_id"],
         start_at=datetime.fromisoformat(slot["start_at"]), idempotency_key="competitor-booking")

    db.add(Message(conversation_id=conversation.id, role="client", text="Подтверждаю"))
    db.commit()
    answer = Agent(FakeLLM([])).reply(db, client, conversation, "Подтверждаю", config)

    assert "время уже заняли" in answer
    assert pending.status == "cancelled"
    assert db.query(Booking).count() == 1


def test_agent_cannot_cancel_another_clients_booking(db, config):
    client = Client(telegram_id="cancel-owner")
    other = Client(telegram_id="cancel-other")
    db.add_all([client, other])
    db.flush()
    conversation = Conversation(client_id=client.id, channel="telegram")
    db.add(conversation)
    db.flush()
    day = date.today() + timedelta(days=2)
    own_slot = find_slots(db, config, "service", day, day, limit=1)[0]
    own = book(db, config=config, client_id=client.id, service_id="service", specialist_id="one",
               start_at=datetime.fromisoformat(own_slot["start_at"]), idempotency_key="cancel-own")
    other_slot = find_slots(db, config, "service", day, day, limit=1)[0]
    foreign = book(db, config=config, client_id=other.id, service_id="service", specialist_id="one",
                   start_at=datetime.fromisoformat(other_slot["start_at"]), idempotency_key="cancel-foreign")

    db.add(Message(conversation_id=conversation.id, role="client", text="Отмени запись"))
    db.commit()
    reply = Agent(FakeLLM([
        tool_call("cancel", {"booking_id": foreign.id}),
        {"role": "assistant", "content": "Не удалось найти вашу запись."},
    ])).reply(db, client, conversation, "Отмени запись", config)
    assert reply == "Не удалось найти вашу запись."
    assert db.get(Booking, foreign.id).status == "confirmed"

    db.add(Message(conversation_id=conversation.id, role="client", text="Отмени мою запись"))
    db.commit()
    Agent(FakeLLM([
        tool_call("cancel", {"booking_id": own.id}),
        {"role": "assistant", "content": "Запись отменена."},
    ])).reply(db, client, conversation, "Отмени мою запись", config)
    assert db.get(Booking, own.id).status == "cancelled"
