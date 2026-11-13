# BIBLE — work-search (jobradar)

> Єдине джерело правди про проєкт. Якщо ви (людина чи AI-агент) продовжуєте роботу,
> прочитайте цей файл повністю, а потім розділ **«10. Статус і наступні кроки»**.
> Оновлюйте його щоразу, коли змінюється архітектура, рішення або статус.
> Останнє оновлення: 2026-11-13.

## 1. Мета

Автоматично відстежувати вакансії керівного рівня в маркетингу, бренді та стратегії на
**djinni.co**, **work.ua** і **robota.ua**, оцінювати їх відповідність профілю власника
(`profile.yaml`) і надсилати в Telegram ті, на які варто відгукнутися. Система працює 24/7
у хмарі (GitHub Actions + Google Apps Script) і повністю безкоштовна.

Цільові ролі разом із синонімами та комбінаціями:
- англійською: Head of Strategy, Head of Marketing, Marketing Director, Director of Marketing, CMO,
  Brand Director, Brand Lead, Brand Leader, Head of Brand, Strategy Director;
- українською: Стратегічний директор, Директор з маркетингу, Маркетинг-директор,
  Бренд-директор, Директор зі стратегії, Керівник відділу маркетингу;
- суміжні: GTM Lead, VP Marketing.

## 2. Архітектура

### 2.1. Конвеєр одного прогону (`jobradar/runner.py`)

```
Apps Script (щогодини) ─▶ GitHub Actions radar.yml ─▶ uv run jobradar run
  0. feedback      getUpdates → натискання ✅/⏭ → vacancies.feedback
  1. collect       collectors/*  → [Vacancy]          (кожне джерело ізольоване)
  2. seen?         SQLite UNIQUE(source, source_id)   → старі пропускаємо
  3. title filter  pipeline/title_match.py            → title_rejected
  4. enrich        collector.enrich(v)                (повний опис, компанія, ЗП, remote)
  5. rules         pipeline/rules.py                  → rule_rejected
  6. dedup         pipeline/dedup.py (компанія+назва) → duplicate
  7. score         pipeline/scorer.py (Gemini | Ollama | Claude | евристика)
                   → scored | error (до 3 спроб); провайдер недоступний → зупинка, решта чекає
  8. notify        notify/telegram.py: кожна вакансія ≥ notify.min_score (55) іде ОДРАЗУ після
                   оцінки окремим повідомленням (назва-посилання, бал, ✔/✖/⚠, 💡, кнопки ✅/⏭);
                   опційно maybe → дайджест у кінці (notify.maybe_as_digest); решта → тихо sent
  9. alert         джерело зламалося → ⚠️ у Telegram (не частіше 1 разу на добу)
```

Статуси в `vacancies.status`: `title_rejected`, `rule_rejected`, `duplicate`, `new`
(очікує скорингу), `error`, `scored` (очікує надсилання), `sent` (надіслано, або тихо
оброблено, якщо бал нижчий за поріг: тоді `tg_message_id IS NULL`).
Поле `feedback` має значення `applied` або `skipped` і заповнюється кнопками в Telegram.
Останні реакції потрапляють у промпт LLM як калібрування (`scoring.feedback_examples`).

### 2.2. Автоматизація (з 13.11.2026, за зразком PhPaster/shafa-watcher)

```
Google Apps Script (tools/trigger.gs, акаунт власника)
   │  щогодини: замок (останній запуск іде або < 45 хв тому → пропуск) → workflow_dispatch
   │  сторож: немає успіху 3 год → ⚠️ у Telegram (раз на 6 год), відновилось → ✅
   ▼
GitHub Actions .github/workflows/radar.yml (ubuntu-latest, concurrency: jobradar)
   restore state ← гілка state (state.db.enc, AES-256)
   PROFILE_YAML secret → profile.yaml
   [requeue, якщо запущено вручну з галочкою]
   uv run --frozen jobradar run
   save state → force-push одного коміту в гілку state (навіть після невдалого прогону)
   ▲
   └─ резерв: schedule "23 */3 * * *"; задача gate пропускає прогін, якщо тригер спрацьовував < 70 хв тому
```

- Репозиторій публічний, тож хвилини Actions безкоштовні. Apps Script теж безкоштовний.
- Розклад GitHub (`schedule`) ненадійний: запізнюється на 5–30 хв, а в shafa-watcher узагалі не
  спрацював за 40 хв. Тому головний будильник — Apps Script, а розклад лише резерв.
- Токен для Apps Script: fine-grained, лише `PhPaster/work-search`, **Actions: Read and write**,
  **без строку дії** (перегенеровано 13.11). Зберігається в Script Properties як `GH_TOKEN`.
  Якщо колись буде токен зі строком, сторож нагадає 🔑 за 14 днів.
- Script Properties: `GH_TOKEN`, `TG_TOKEN`, `TG_CHAT`. Функції: `setup` (створює щогодинний
  тригер + перший dispatch), `dispatch` (функція тригера), `check` (діагностика + 🧪 у Telegram), `stop`.
  Допоміжні функції мають суфікс `_`: Apps Script показує в списку запуску навіть `const`-стрілочні
  функції, а запуск такої нічого не робить (власник якось запустив `props` замість `check`).
- **Стан**: SQLite → gzip → AES-256-CBC (PBKDF2, 200k ітерацій, SHA-256) → `state.db.enc` в
  orphan-гілці `state` (завжди один коміт). Скрипт: `scripts/state.sh pack|unpack|pull|push`.
  Ключ — `secrets.STATE_KEY`, копія в локальному `.env`. Шифрування, бо репозиторій публічний, а в
  базі є аналізи, що спираються на досвід власника. `unpack` перевіряє `PRAGMA integrity_check`;
  якщо розшифрування не вдалося, прогін падає й **не** перезаписує стан порожньою базою.
- Секрети репозиторію: `GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `PROFILE_YAML`, `STATE_KEY`.
- Мінімальна частота — раз на годину: Djinni отримує ~16 RSS-запитів за прогін плюс сторінки нових вакансій.

### 2.3. Ліміти безкоштовного Gemini (`jobradar/budget.py`, `pipeline/scorer.py`)

- Кожен HTTP-запит до Gemini (зокрема повтори на 503) рахується в `meta.llm_used` за
  «тихоокеанський» день, бо RPD free tier скидається опівночі за Pacific (10:00 за Києвом).
- `gemini.max_requests_per_day: 150`: коли ліміт вичерпано, оцінка зупиняється, а вакансії
  лишаються в `new` і оцінюються наступного дня.
- Якщо Google відповідає 429 на всіх моделях (реальна квота скінчилася раніше за наш ліміт),
  оцінка стає на паузу до 00:05 Pacific (`meta.llm_paused_until`), і в Telegram надходить одне ⏸.
- На 503 «high demand», який трапляється часто: 3 спроби на модель з паузою 5→10 с → запасна модель
  `gemini-3.1-flash-lite` → `RateLimited` (зупинка до наступного прогону без витрачання спроб вакансії).
- Між запитами пауза 6 с (≤10 RPM). Не більше 25 оцінок за прогін (`scoring.max_per_run`).
- Збір вакансій (Djinni) працює завжди, навіть на паузі, тому нічого не губиться.

## 3. Ключові рішення

| Рішення | Чому |
|---|---|
| Python 3.12 + uv, SQLite, без фреймворків | Мінімум залежностей; той самий код працює локально й у GitHub Actions. |
| **Хмара: GitHub Actions + Apps Script-будильник** (рішення власника 13.11) | Працює 24/7 без ноутбука. Python-код не переписується. Безкоштовно для публічного репо. Перевірена схема з shafa-watcher. Чистий Apps Script (JS) відкинуто: довелося б переписати весь код, а ліміт 6 хв на виконання замалий для прогону з паузами Gemini (3–5 хв, з повторами довше). |
| Стан зашифрований у гілці `state` | Між прогонами Actions треба пам'ятати, що вже бачили й надіслали. Гілку публічного репо бачать усі, тому база шифрується. |
| launchd на Mac відкинуто | Проєкт лежить у `~/Documents`, а macOS TCC не пускає туди фонові задачі (`Operation not permitted`). Знадобився б Full Disk Access для `/bin/bash`, і ще залежність від увімкненого ноутбука. Шаблон лишився як запасний варіант. |
| Djinni читається через **RSS** | Офіційний публічний фід із повним описом, без парсингу HTML. З IP GitHub працює. |
| Work.ua і Robota.ua читаються через **e-mail-розсилки (IMAP)** | Обидва сайти віддають не-браузерним клієнтам Cloudflare JS-challenge (403). Обходити захист від ботів ми **свідомо не будемо**. Розсилки ж є легальним офіційним каналом. Деталі в §4. |
| Двоступеневий фільтр: regex по назві → LLM | Пошук на сайтах дає багато шуму (~80% відсіюється назвою), а LLM викликається лише для релевантного. |
| Кожна вакансія оцінюється LLM лише раз | Стан зберігається, тому повторні прогони дешеві. Після зміни профілю є `requeue`. |
| **Пріоритет безкоштовних інструментів** (рішення власника) | `scoring.provider: auto` обирає Gemini (безкоштовний тариф), якщо є `GEMINI_API_KEY`; інакше Claude, якщо є ключ Anthropic; інакше евристику. Ollama вмикається явно. |
| Gemini: REST `generateContent`, `gemini-3.5-flash-lite`, `responseMimeType` + `responseJsonSchema` | На безкоштовному тарифі модель «Free of charge», і Google радить її для дешевих масових задач. JSON-схема примусова (перевірено вживу). Якщо API відхилить `responseJsonSchema`, скорер сам переходить у простий JSON-режим. `generateContent` у документації названо legacy, але він підтримується; рекомендованим є Interactions API. |
| Ollama: `/api/chat`, `format` = JSON-схема, `gemma3:4b` (3.4 ГБ) | Повністю локально і безкоштовно. Маленька модель, бо диск власника заповнений на 96%. Кращі варіанти: `qwen3:8b` (5.2 ГБ), `gemma3:12b` (8.2 ГБ). У хмарі не використовується. |
| Claude `claude-opus-5-5`, effort `medium`, structured output | Платний варіант. `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`). Налаштування в розділі `claude:` конфігу. |
| Спільний промпт, схема і `result_from_data` для всіх LLM | Однакова логіка вердикту: пороги з конфігу, а «skip» від моделі (стоп-фактор) завжди перемагає. |
| Промпт: усі текстові поля українською, `cover_hook` обов'язково з конкретним кейсом із профілю | Перша версія давала англійські `matched` і загальний `cover_hook`. |
| Гемблінг — штраф, а не фільтр (рішення власника 13.11) | У CV є Parimatch; на Djinni це ~половина Head of Marketing. Модель знімає ~15 балів за `scoring_guidance`. |
| Повідомлення в Telegram одразу після оцінки, кожне окремо (рішення власника) | Спершу вони йшли в кінці прогону, і власник 5 хв нічого не бачив. |
| Telegram через сирий Bot API (`httpx`) | Бібліотека не потрібна. Кнопки читаються через `getUpdates` на початку кожного прогону, тож постійно запущений бот-процес не потрібен. |
| Профіль без контактів | Профіль іде в Gemini free tier, а там Google може використовувати контент. |
| Чесний User-Agent і пауза ≥2 с між запитами до одного хоста | Ввічливість; robots.txt Djinni дозволяє `/jobs/`. |
| `profile.yaml`, `.env`, `data/`, `state.db.enc` у `.gitignore` | Репозиторій публічний. |

## 4. Як влаштовані сайти (дослідження 2026-11-13)

### Djinni (djinni.co) — працює ✅
- RSS: `https://djinni.co/jobs/rss/?<params>`, тобто ті самі фільтри, що й у пошуку на сайті.
  - `all_keywords=...`: повнотекстовий пошук, усі слова через AND (не тільки в назві).
  - `primary_keyword=Marketing`: уся категорія Marketing, ~100 останніх вакансій. Це наш catch-all.
  - Пошук кирилицею поки повертає 0: майже всі вакансії Djinni англійською.
- Item містить `title`, `link` (`/jobs/<id>-slug/`), `description` (повний HTML), `pubDate`, `category`.
  **Компанії в RSS немає.**
- Сторінка вакансії містить `<script type="application/ld+json">` з `JobPosting`:
  `hiringOrganization.name`, `jobLocationType: TELECOMMUTE`, `baseSalary` (якщо вказана), `jobLocation`.
- `robots.txt` забороняє лише `/jobs2`, `/q`, `/developers`, `/free-jobs`, `/set_lang`.
- Перший живий прогін: 16 пошуків → 197 унікальних вакансій → 39 пройшли фільтр назви → 23 оцінено.
  Збір триває ~30 с (пауза 2 с), а з оцінкою й повторами Gemini — 2–5 хв.
- З IP GitHub Actions працює (13.11: 193 вакансії).

### Work.ua — Cloudflare ⛔ → e-mail-розсилки
- Будь-яка сторінка (`/jobs-<запит>/`, `/jobs/<id>/`) віддає HTTP 403 «Перевірка надійності
  підключення» (Cloudflare challenge). `robots.txt` при цьому відкритий.
- Публічного API чи RSS для пошукувачів немає (`api.work.ua` → 501).
- Канал, який ми використовуємо: підписка на нові вакансії за пошуковим запитом на e-mail.
  Посилання на вакансії мають вигляд `work.ua/jobs/<id>/`.

### Robota.ua — Cloudflare ⛔ → e-mail-розсилки
- `robota.ua/*`, `api.robota.ua/*`, `employer-api.robota.ua` віддають 403 Cloudflare challenge.
- `dracula.robota.ua` (GraphQL їхнього фронтенду) відповідає без challenge, але це
  **неофіційний API**. Інтроспекцію заблокував агент безпеки як несанкціонований доступ.
  Використовувати його можна лише з явного рішення власника. Ризики: умови користування, нестабільність.
- Канал, який ми використовуємо: e-mail-підписка. Посилання мають вигляд `robota.ua/company<cid>/vacancy<id>`;
  часто вони загорнуті в трекінгові редиректи, тому парсер розкодовує URL.

### Парсер e-mail-розсилок (`collectors/email_alerts.py`)
- Підключення через IMAP SSL, тека в режимі **read-only** (листи не стають прочитаними),
  пошук `SINCE <N днів> FROM "work.ua" | "robota.ua"`.
- HTML лінеаризується в послідовність текстових фрагментів і посилань. Посилання на вакансію визначається
  регекспом (зокрема після url-decode трекінгових редиректів). Назва береться з тексту першого
  «змістовного» посилання; кнопки на кшталт «Детальніше» чи «Відгукнутися» та картинки пропускаються.
  Сніпет — це текст до наступної вакансії (компанія, місто, ЗП). Зарплата парситься регекспом.
- ⚠ **Перевірено лише на синтетичних листах** (`tests/fixtures/email_*.eml`). Справжня верстка
  листів невідома. Після першого реального листа його треба зберегти як фікстуру (без
  персональних даних) і підправити парсер, якщо він помиляється.
- Повного опису немає, тому `description_is_snippet=True`, і LLM про це знає.
- **Поки вимкнено** (`email_alerts.enabled: false`). Для хмари знадобляться секрети
  `IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD` і передача їх у `env` у `radar.yml`.

## 5. Структура репозиторію

```
BIBLE.md                 цей файл
README.md                швидкий старт і команди
SETUP.md                 покрокове налаштування для власника-початківця (усе безкоштовно)
config.yaml              пошуки, фільтри, пороги, провайдер LLM, ліміти (без секретів)
profile.example.yaml     шаблон профілю → копія в profile.yaml (git-ignored)
.env.example             ключі → копія в .env (git-ignored)
.github/workflows/radar.yml   хмарний прогін: gate (резервний розклад) + radar (стан → run → стан)
tools/trigger.gs         Google Apps Script: щогодинний workflow_dispatch + сторож у Telegram
scripts/state.sh         шифрований стан: pack | unpack | pull | push
scripts/run.sh           один локальний прогін із логом (запасний варіант)
scripts/com.jobradar.run.plist.template   launchd (запасний варіант; див. TCC у §3)
jobradar/
  cli.py                 команди: run, feedback, tg-setup, tg-test, stats, show, rescore,
                         requeue, llm-check; run-lock (fcntl) для локальних запусків
  runner.py              оркестрація прогону (RunReport, process_feedback, негайне надсилання)
  budget.py              DailyBudget: денний ліміт запитів LLM за Pacific-днем, пауза до скидання
  config.py              завантаження config/profile/.env → Settings
  db.py                  SQLite: vacancies + meta (tg_offset, last_run, last_collector_alert,
                         llm_day, llm_used, llm_paused_until)
  models.py              Vacancy, ScoreResult
  http.py                PoliteClient: rate limit на хост, retries, чесний UA
  text.py                html_to_text, normalize
  collectors/base.py     Collector (collect, enrich), CollectorError
  collectors/djinni.py   RSS + JSON-LD
  collectors/email_alerts.py  Work.ua/Robota.ua з IMAP
  pipeline/title_match.py     роль+домен у вікні N слів, точні C-level, виключення
  pipeline/rules.py      стоп-слова, мін. ЗП (USD), макс. вік
  pipeline/dedup.py      ключ «компанія|назва» для крос-сайтового дедупу
  pipeline/scorer.py     GeminiScorer (REST, retries, fallback model), OllamaScorer,
                         LLMScorer (Claude, Anthropic SDK), HeuristicScorer, RateLimited
  notify/telegram.py     форматування (HTML), кнопки, getUpdates
tests/                   86 тестів pytest, офлайн; fixtures/ — урізаний реальний RSS Djinni,
                         JSON-LD, синтетичні листи
```

## 6. Конфігурація і секрети

- **GitHub Secrets** (хмара): `GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
  `PROFILE_YAML` (вміст `profile.yaml`), `STATE_KEY`.
- **Apps Script → Script Properties**: `GH_TOKEN`, `TG_TOKEN`, `TG_CHAT`.
- **`.env`** (локально): `GEMINI_API_KEY` (або `GOOGLE_API_KEY`), `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_CHAT_ID`, `STATE_KEY` (⚠ єдина локальна копія ключа стану, не губити),
  `IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD` (для Gmail потрібен *пароль застосунку*), опційно `JOBRADAR_DB`.
- `config.yaml`, найважливіше:
  - `djinni.searches`: список параметрів RSS;
  - `title_filter.exclude`: агресивні виключення (performance, smm, crm, retention, affiliate…).
    Через них «Chief Marketing/Retention Officer» теж відсіюється. Це свідомо, але правиться тут;
  - `rules.exclude_keywords: []`: гемблінг **не відсіюється**; натомість `profile.yaml → scoring_guidance`
    просить модель знімати ~15 балів, а евристика знімає 15 балів за `keywords.penalty`;
  - `rules.max_age_days: 21`, `rules.min_salary_usd: null`;
  - `scoring.provider`: `auto | gemini | ollama | claude | heuristic`; `scoring.max_per_run: 25`;
    `scoring.thresholds` (apply ≥80, maybe ≥55);
  - `gemini.model: gemini-3.5-flash-lite`, `fallback_models: [gemini-3.1-flash-lite]`, `retries: 3`,
    `min_interval_sec: 6`, `max_requests_per_day: 150`;
  - `notify.min_score: 55`, `notify.send_maybe: true`, `notify.maybe_as_digest: false`.
- `profile.yaml` повністю потрапляє в системний промпт. Блок `keywords` використовується тільки евристикою.
  Поточний профіль (13.11.2026) складено з `~/Documents/CV_Paster_master_2026_UA.odt` (найсвіжіше
  резюме: досвід TWID, TRENDFORMER, TBWA, banda, GRAPE, Dentsu; компетенції; освіта) + кейсів
  з paster.pro + відповідей власника:
  - формат: віддалено, Київ гібрид/офіс, інші міста України; переїзд за кордон — ні;
  - мінімальної зарплати немає; гемблінг дозволений з нижчим пріоритетом.
  **Контактів (телефон, e-mail, LinkedIn) навмисно немає.**
  Розділи: `summary`, `experience`, `selected_cases` (з них модель бере `cover_hook`),
  `competencies`, `industries`, `preferences`, `scoring_guidance` (правила оцінки), `deal_breakers`, `keywords`.

## 7. Як запускати і тестувати

Покрокова інструкція для власника: **[SETUP.md](SETUP.md)**.

```bash
uv sync
uv run pytest                                    # 86 тестів, офлайн
uv run jobradar llm-check                        # один виклик LLM на тестовій вакансії
uv run jobradar --db /tmp/t.sqlite3 run --dry-run --no-llm   # живий Djinni в окрему базу, без витрат
scripts/state.sh pull                            # завантажити хмарний стан у data/ (стара → .bak)
uv run jobradar stats                            # після pull: статистика, запити Gemini сьогодні
gh workflow run radar.yml                        # запустити хмарний прогін вручну
gh workflow run radar.yml -f requeue=true        # … з переоцінкою після зміни профілю
gh run list --workflow radar.yml --limit 5       # останні хмарні прогони
```

- ⚠ **Не запускати `jobradar run` на ноутбуці проти локальної бази**, поки працює хмара: у локальної
  бази інша пам'ять, і вакансії прийдуть удруге. Якщо дуже треба: `state.sh pull` → `run` →
  `state.sh push`, і лише коли в хмарі не йде прогін.
- `tests/test_llm_sdk.py` проганяє `LLMScorer` через **справжній** Anthropic SDK з
  підміненим транспортом. Нюанс: SDK 2.x працює на `httpx2`, тож моки треба робити через `httpx2.MockTransport`.
- Gemini/Ollama-тести (`tests/test_free_providers.py`) використовують `httpx.MockTransport`.

## 8. Експлуатація: що робити, коли щось не так

| Симптом | Що перевірити / зробити |
|---|---|
| ⚠️ від сторожа «успішного запуску не було N год» | https://github.com/PhPaster/work-search/actions: червоний прогін → відкрити лог. Немає запусків → Apps Script `check` (401 = токен, 404 = репозиторій не вибрано в токені). |
| Apps Script `check`: `repository: 401` | `GH_TOKEN` недійсний (перегенеровано, вставлено не повністю, не збережено). Новий fine-grained токен → Script Properties → Зберегти. |
| ⚠️ «джерела не відповіли» | Djinni змінив RSS або заблокував IP GitHub. Перевірити локально `--dry-run --no-llm`. |
| ⏸ «ліміт Gemini вичерпано» | Нічого: оцінка відновиться після 10:00 за Києвом. Якщо так щодня, зменшити `max_requests_per_day` або підняти поріг фільтра. |
| Прогін червоний на «Restore state» | Не збігається `STATE_KEY` або зіпсовано `state.db.enc`. Стан не перезаписується. Перевірити секрет; крайній захід — видалити гілку `state` (тоді прийдуть дублікати старих вакансій). |
| Забагато «на межі» | Підняти `notify.min_score` (55 → 65–70) або уточнити `scoring_guidance`. |
| Змінили профіль | `gh secret set PROFILE_YAML < profile.yaml` → Actions → Run workflow з галочкою requeue. |
| Gemini постійно 503 | Норма для free tier у пікові години; повтори й запасна модель це покривають. Можна додати ще модель у `fallback_models`. |

## 9. Вартість

**Усе безкоштовно:** GitHub Actions (публічний репозиторій), Google Apps Script, Djinni RSS, Telegram,
Gemini на безкоштовному тарифі (Gmail IMAP і Ollama, якщо знадобляться, теж безкоштовні).
- Gemini free: ліміти встановлюються на проєкт і публікуються лише в AI Studio
  (https://aistudio.google.com/rate-limit). За даними розробників на осінь 2026 року, для Flash-Lite
  це приблизно 15 RPM і 500 RPD, а для Flash лише ~20 RPD. Звичайного дня треба 5–20 запитів.
  На безкоштовному тарифі Google використовує контент для покращення продуктів; у промпт
  потрапляють публічні вакансії й профіль без контактів.
- Платний варіант (Claude, `claude-opus-5-5`, $4 / $20 за 1M): ~3–5K вхідних і ~1–2K вихідних токенів
  на вакансію, тобто ≈ $0.03–0.06 за вакансію і кілька доларів на місяць (оцінка, не вимір).

## 10. Статус і наступні кроки

**Працює (13.11.2026):**
- [x] Хмарний конвеєр: Apps Script щогодини → GitHub Actions → Djinni → Gemini → Telegram.
      Перший хмарний прогін успішний (run 43434662387): 193 вакансії, стан відновлено й збережено, дублікатів немає.
- [x] Apps Script: `setup` створив щогодинний тригер, `check` → `repository: 200`, `token expires: never`,
      🧪 прийшов у Telegram.
- [x] Колектор Djinni (RSS + JSON-LD), фільтр назв (перевірено на ~200 живих назвах), правила, дедуп.
- [x] Gemini free tier вживу: `gemini-3.5-flash-lite`, JSON-схема приймається, відповіді українською з
      конкретними кейсами. Повтори на 503, запасна модель, денний бюджет, пауза на квоті.
- [x] Профіль із CV (жовтень 2026), гемблінг зі штрафом, `requeue`.
- [x] Telegram: окреме повідомлення одразу після оцінки, кнопки ✅/⏭ → feedback → калібрування.
- [x] Локальні прогони власника 13.11: 6 apply і 21 maybe надіслано (з них 4 + 6 ще на першому профілі).
- [x] 86 тестів; SETUP.md для початківця; launchd на Mac вимкнено, plist видалено.

**Ще не перевірено вживу:**
- [ ] Gemini саме в хмарі: першого хмарного прогону нових релевантних вакансій не було; секрет задано.
- [ ] Реальні листи Work.ua/Robota.ua (потрібні підписки, IMAP і секрети в хмарі).
- [ ] Ollama (не встановлено) і Claude (немає ключа) — запасні провайдери.

**Наступні кроки:**
1. Власник: натискати ✅/⏭ під вакансіями (калібрує модель).
2. Через тиждень: `scripts/state.sh pull` → `jobradar stats`; чи збігаються бали з ✅/⏭; за потреби
   підняти `notify.min_score` або підкрутити `scoring_guidance`.
3. Підписки на work.ua і robota.ua (запити: «маркетинг директор», «head of marketing», «бренд директор»,
   «стратегічний директор», «CMO»), пароль застосунку Gmail, секрети `IMAP_*` у GitHub + `env` у `radar.yml`,
   `email_alerts.enabled: true`.
4. Зберегти реальний лист кожного сайту як фікстуру й відкалібрувати парсер.
5. (Опційно, за рішенням власника) колектор robota.ua через `dracula.robota.ua`, див. ризики в §4.
6. Ідеї: щотижневий звіт зі статистикою відгуків; кнопка «згенеруй супровідний лист».
