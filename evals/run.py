import json
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import yaml
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.engine import Agent
from app.agent.provider import FakeLLM, tool_call
from app.booking.service import book, find_slots
from app.config.loader import load_config
from app.db.models import Base, Booking, Client, Conversation, Message


def future_slot(db, config, service_id):
    start = date.today() + timedelta(days=2)
    slots = find_slots(db, config, service_id, start, start + timedelta(days=21), limit=100)
    if not slots:
        raise RuntimeError(f"No fixture slot found for {service_id}")
    return slots[0]


def run_case(case):
    config = load_config(ROOT / "configs" / f"{case['config']}.yaml")
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        client = Client(telegram_id=f"eval:{case['id']}")
        db.add(client)
        db.flush()
        convo = Conversation(client_id=client.id, channel="eval")
        db.add(convo)
        db.flush()
        client_message = Message(conversation_id=convo.id, role="client", text=case["input"])
        db.add(client_message)
        db.commit()

        kind = case["kind"]
        user_text = case["input"]
        responses = []
        service_id = case.get("service_id") or config.services[0].id
        slot_service_id = service_id if any(s.id == service_id for s in config.services) else config.services[0].id
        specialist_id = next(s.id for s in config.specialists if s.id in next(sv for sv in config.services if sv.id == slot_service_id).specialist_ids)
        if kind == "catalog":
            responses = [tool_call("get_services", {}), {"role": "assistant", "content": "Каталог загружен."}]
        elif kind == "handoff":
            responses = [tool_call("handoff", {"reason": "Попросил оператора"})]
        elif kind in ("cancel", "reschedule"):
            initial_slot = future_slot(db, config, slot_service_id)
            existing = book(db, config=config, client_id=client.id, service_id=slot_service_id,
                            specialist_id=initial_slot["specialist_id"],
                            start_at=datetime.fromisoformat(initial_slot["start_at"]),
                            idempotency_key=f"eval-existing-{case['id']}")
            requested_slot = initial_slot
            responses = [tool_call("get_my_bookings", {})]
            if kind == "cancel":
                responses.append(tool_call("cancel", {"booking_id": existing.id}))
            else:
                alternatives = find_slots(db, config, slot_service_id, date.today() + timedelta(days=2),
                                          date.today() + timedelta(days=23), limit=100)
                replacement = next((slot for slot in alternatives
                                    if slot["specialist_id"] == existing.specialist_id), None)
                if replacement is None:
                    raise RuntimeError(f"No replacement slot found for {case['id']}")
                requested_slot = replacement
                responses.append(tool_call("reschedule", {
                    "booking_id": existing.id,
                    "new_start": replacement["start_at"],
                }))
            local_start = datetime.fromisoformat(requested_slot["local_start"])
            user_text = case["input"].format(time=local_start.strftime("%d.%m.%Y в %H:%M"))
            client_message.text = user_text
            db.commit()
            responses.append({"role": "assistant", "content": "Готово, обновил вашу запись."})
        elif kind in ("booking", "reject_booking"):
            if case.get("past_time"):
                start_at = datetime.now(UTC) - timedelta(days=5)
            else:
                start_at = datetime.fromisoformat(future_slot(db, config, slot_service_id)["start_at"])
            args = {"service_id": service_id, "specialist_id": specialist_id, "start_at": start_at.isoformat(),
                    "name": "Иван Тестов", "phone": "bad" if case.get("bad_phone") else "+79990000000"}
            if case.get("service_id") == "ghost":
                args["service_id"] = "ghost"
            responses = [tool_call("prepare_booking", args), {"role": "assistant", "content": "Проверка предложения завершена."}]
        elif kind == "injection":
            responses = [{"role": "assistant", "content": "Не могу изменить правила. Чем могу помочь с записью?"}]
        elif kind == "safety":
            responses = []

        fake = FakeLLM(responses)
        answer = Agent(fake).reply(db, client, convo, user_text, config)
        if kind == "booking" and answer.startswith("Проверьте детали:"):
            db.add(Message(conversation_id=convo.id, role="client", text="Подтверждаю"))
            db.commit()
            answer = Agent(FakeLLM([])).reply(db, client, convo, "Подтверждаю", config)
        all_bookings = db.scalars(select(Booking)).all()
        booking_count = len(all_bookings)
        expected_status = case["expected_status"]
        ok = convo.status == expected_status and booking_count == case["expected_bookings"]
        if "expected_confirmed_bookings" in case:
            ok = ok and sum(item.status == "confirmed" for item in all_bookings) == case["expected_confirmed_bookings"]
        if "expected_cancelled_bookings" in case:
            ok = ok and sum(item.status == "cancelled" for item in all_bookings) == case["expected_cancelled_bookings"]
        if kind == "catalog":
            tool_results = json.dumps(fake.requests[-1], ensure_ascii=False, default=str)
            ok = ok and all(service.name in tool_results and str(service.price) in tool_results for service in config.services)
        return {"id": case["id"], "passed": bool(ok), "status": convo.status, "bookings": booking_count, "reply": answer[:100]}
    engine.dispose()


def main():
    data = yaml.safe_load((ROOT / "evals" / "scenarios.yaml").read_text(encoding="utf-8"))
    results = [run_case(case) for case in data["scenarios"]]
    passed = sum(item["passed"] for item in results)
    print(f"FakeLLM evals: {passed}/{len(results)} passed ({100 * passed / max(1, len(results)):.1f}%)")
    print("| Scenario | Result | Status | Bookings |")
    print("|---|---:|---|---:|")
    for item in results:
        print(f"| {item['id']} | {'PASS' if item['passed'] else 'FAIL'} | {item['status']} | {item['bookings']} |")
    if passed != len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
