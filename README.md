# SlotPilot

Конфигурируемый AI-администратор записи для клиники или барбершопа. Код проверяет доступность времени, цену и правила бронирования; LLM отвечает за формулировки и может вызывать только ограниченный набор инструментов.

> Это портфолио-демо. Пока не подключены Telegram и LLM-ключ, HTTP API и тесты работают локально, а диалоговый endpoint показывает каталог и сообщает, что нужен LLM-провайдер.

## Возможности

- Один движок и два YAML-профиля: клиника и барбершоп.
- Поиск времени по расписанию, услуге, длительности, часовому поясу и занятым записям.
- Запись, отмена и атомарный перенос. PostgreSQL защищает от пересекающихся записей ограничением GiST, повторные запросы — ключом идемпотентности.
- Переключаемый OpenAI-совместимый LLM API и `FakeLLM` для воспроизводимых проверок.
- Двухэтапная проверка безопасности: известные красные флаги сразу передаются человеку, неоднозначные сообщения о симптомах проходят осторожную классификацию; при сбое или сомнении диалог передаётся администратору. Телефоны, email и распознанные ФИО маскируются перед LLM.
- Telegram через long polling или webhook.
- Получение будущих записей клиента перед их отменой или переносом.
- Админка с авторизацией, списком диалогов, срочной подсветкой, перехватом, возвратом боту и ответом клиенту.
- История и записи в БД; синхронизация Google Calendar идёт через outbox и повторяется отдельно от бронирования.
- 32 сценария FakeLLM для стандартных случаев, красных флагов, инъекций, передачи человеку и отказов записи.

## Архитектура

```mermaid
flowchart LR
    TG[Telegram polling / webhook] --> API[FastAPI]
    API --> SAFE[Safety и маскирование]
    SAFE --> AGENT[Агент / LLMProvider]
    AGENT --> TOOLS[Белый список инструментов]
    TOOLS --> BOOK[BookingService]
    BOOK --> DB[(PostgreSQL — источник истины)]
    DB --> OUTBOX[Calendar outbox]
    OUTBOX --> CAL[Google Calendar mirror]
    API --> ADMIN[Защищённая админка]
```

## Быстрый запуск через Docker

1. Установите и запустите Docker Desktop, затем скопируйте `.env.example` в `.env` и задайте уникальные буквенно-цифровые `POSTGRES_PASSWORD` и `ADMIN_PASSWORD`.
2. Для реального диалога задайте `TELEGRAM_BOT_TOKEN` и `LLM_API_KEY`. Провайдер можно заменить через `LLM_BASE_URL` и `LLM_MODEL`.
3. Из папки проекта выполните:

```powershell
docker compose up --build
```

API будет доступен на `http://localhost:8000`; корневой адрес перенаправляет в админку `/admin`. Проверка состояния — `/health`, каталог услуг — `/api/services`. Порты по умолчанию привязаны к loopback-интерфейсу. Для webhook за reverse proxy задайте `APP_HOST=0.0.0.0`. Пока `ADMIN_PASSWORD` пуст, админка не открывает данные. По умолчанию панель использует HTTP Basic с логином `admin`; задайте собственный `ADMIN_USERNAME`.

Для Telegram long polling в отдельном окне:

```powershell
docker compose --profile telegram-polling up --build
```

Polling запускается отдельным контейнером и требует `TELEGRAM_BOT_TOKEN`. Для webhook задайте `TELEGRAM_WEBHOOK_SECRET`, настройте HTTPS-адрес Telegram и используйте `/telegram/webhook`.

### Локальная модель через LM Studio

1. Загрузите модель в LM Studio, откройте **Model Server** и запустите сервер на порту `1234`.
2. В `.env` укажите OpenAI-совместимый адрес, который виден из Docker:

```dotenv
LLM_API_KEY=lm-studio
LLM_BASE_URL=http://host.docker.internal:1234/v1
LLM_MODEL=openai/gpt-oss-20b
```

`LLM_MODEL` должен совпадать с идентификатором модели из `http://localhost:1234/v1/models`. Если в LM Studio включена авторизация, вместо `lm-studio` задайте её API key. Не используйте `localhost` в `LLM_BASE_URL`: внутри контейнера это адрес самого контейнера, а `host.docker.internal` ведёт к LM Studio на компьютере.

Чтобы выбрать барбершоп вместо клиники, задайте `BUSINESS_CONFIG=barbershop` в `.env` и перезапустите сервис.

## Локальная разработка

Нужен Python 3.14 (допустим 3.13). В PowerShell:

```powershell
cd C:\dev\slotpilot
.venv\Scripts\Activate.ps1
python -m pip check
python -m alembic upgrade head
python -m uvicorn app.main:app --reload
```

Локально по умолчанию используется SQLite для быстрого старта. Для общей разработки и конкурентного бронирования используйте PostgreSQL через Docker. В PowerShell-процессе `DATABASE_URL` можно задать явно, например `postgresql+psycopg://slotpilot:пароль@localhost:5432/slotpilot`.

## Проверки

```powershell
python -m pytest -q
python evals\run.py
```

Текущий результат: **31 тест** и **32/32 FakeLLM-сценария**. Сценарии включают безопасность, инъекции, каталог, передачу человеку, отклонение некорректных записей и успешные бронирования для обеих конфигураций. Это проверяет бизнес-правила и обработку заготовленных ответов, но не измеряет качество реальной модели. Live-бот требует настроенных Telegram и LLM в `.env`. Стоимость и задержка реальных разговоров не включены в FakeLLM-отчёт.

## Настройка интеграций

### Telegram

1. В Telegram откройте `@BotFather`, выполните `/newbot`, выберите имя и username.
2. Скопируйте выданный токен в `TELEGRAM_BOT_TOKEN` файла `.env`. Не публикуйте его и не присылайте в чат.
3. Для разработки используйте polling. Для webhook нужен публичный HTTPS URL и случайный секрет `TELEGRAM_WEBHOOK_SECRET`.

### LLM

Задайте ключ совместимого Chat Completions API в `LLM_API_KEY`, базовый URL в `LLM_BASE_URL`, модель в `LLM_MODEL`. Не передавайте реальные медицинские данные. Провайдер получает маскированный текст; сообщения и карточка клиента остаются в вашей БД.

### Google Calendar

База остаётся источником истины, Google Calendar — зеркалом. Для личного теста подключается Google-аккаунт, который владеет календарём.

1. В Google Cloud Console создай проект и включи **Google Calendar API**.
2. В **Google Auth Platform → Audience** выбери `External` и добавь свой Google-аккаунт в **Test users**. Пока приложение в режиме тестирования, refresh token для Calendar scope истекает через 7 дней — для постоянного использования OAuth-приложение нужно перевести в production; Google может запросить верификацию для внешних пользователей.
3. В **Google Auth Platform → Clients** создай OAuth client типа **Web application**. Добавь Authorized redirect URI `https://developers.google.com/oauthplayground`. [Playground требует Web client и этот redirect URI](https://developers.google.com/admob/api/v1/how-tos/playground).
4. Открой [OAuth 2.0 Playground](https://developers.google.com/oauthplayground), нажми шестерёнку, включи **Use your own OAuth credentials**, выбери server-side flow и offline access, затем введи Client ID и Client secret.
5. В поле собственных scopes вставь `https://www.googleapis.com/auth/calendar.events.owned`, нажми **Authorize APIs**, выбери тот же Google-аккаунт, а в Step 2 — **Exchange authorization code for tokens**. Скопируй показанный refresh token. `calendar.events.owned` достаточно для календаря, которым этот аккаунт владеет; для календаря, которым аккаунту поделились, используй `https://www.googleapis.com/auth/calendar.events`.
6. В Google Calendar открой **Настройки и доступ** нужного календаря → **Интеграция календаря** и возьми **Идентификатор календаря**. Для основного личного календаря оставь `primary`.
7. Добавь значения в локальный `.env`:

```dotenv
GOOGLE_CALENDAR_ID=primary
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_REFRESH_TOKEN=...
```

Не присылай секреты в чат и не добавляй `.env` в Git. Приложение обновляет access token автоматически. Запусти синхронизацию:

```powershell
docker compose --profile google-calendar up -d --build calendar-sync
docker compose --profile google-calendar logs -f calendar-sync
```

Остановка просмотра логов через `Ctrl+C` не останавливает контейнер. Если Google недоступен, подтверждённая запись остаётся в PostgreSQL, а outbox повторяет синхронизацию. OAuth-данные храни только в локальном `.env` или secret manager. Scope определяет, к каким календарям можно обращаться; для более узких прав выбирай минимальный scope, который соответствует календарю.

## Данные и ограничения

- Примеры расписания и услуг синтетические. Не используйте демо для реальных пациентов или клиентов без отдельной оценки безопасности, согласий и требований законодательства.
- В клиническом профиле известные слова из `red_flags` сразу передают диалог человеку. Сообщения с возможными симптомами дополнительно проверяет выбранная локальная или облачная LLM; при ошибке или сомнительном результате включается передача человеку. Классификатор может ошибаться: система не ставит диагноз, не заменяет медицинского специалиста или экстренную помощь.
- Телефоны, email и распознаваемые последовательности ФИО маскируются только перед LLM. Исходные сообщения сохраняются в БД для истории администрирования. Для эксплуатации задайте срок хранения, ограничьте доступ, настройте резервные копии и шифрование.
- Для работы в РФ отдельно проверьте применимость 152-ФЗ, локализацию, согласия и правомерность передачи данных выбранному LLM-провайдеру.
- HTTP Basic подходит только для закрытой демо-среды за HTTPS. Для публичного сервиса добавьте полноценную учётную запись администратора, управление сессиями, CSRF-защиту и аудит действий.
- Google Calendar — зеркало. В проекте пока нет Yclients/Altegio адаптера; для конкретной МИС нужен её API и тестовая учётная запись.
- В демонстрации нет готовой публичной инфраструктуры, домена, TLS-сертификата, выданного Telegram-токена или подключённого LLM-аккаунта.

## Структура

```text
app/            API, агент, safety, запись, календарь, модели и Telegram polling
configs/        clinic.yaml и barbershop.yaml
evals/          32 YAML-сценария и FakeLLM runner
migrations/     Alembic migration и PostgreSQL exclusion constraint
tests/          тесты конфигурации, записи, безопасности и API-функций
```
