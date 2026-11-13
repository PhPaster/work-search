# BIBLE — work-search (jobradar)

> Єдине джерело правди про проєкт. Якщо ви (людина чи AI-агент) продовжуєте роботу,
> прочитайте цей файл повністю, а потім розділ **«9. Статус і наступні кроки»**.
> Оновлюйте його щоразу, коли змінюється архітектура, рішення або статус.

## 1. Мета

Автоматично відстежувати вакансії керівного рівня в маркетингу, бренді та стратегії на
**djinni.co**, **work.ua** і **robota.ua**, оцінювати їх відповідність профілю власника
(`profile.yaml`) і надсилати в Telegram ті, на які варто відгукнутися.

Цільові ролі разом із синонімами та комбінаціями:
- англійською: Head of Strategy, Head of Marketing, Marketing Director, Director of Marketing, CMO,
  Brand Director, Brand Lead, Brand Leader, Head of Brand, Strategy Director;
- українською: Стратегічний директор, Директор з маркетингу, Маркетинг-директор,
  Бренд-директор, Директор зі стратегії, Керівник відділу маркетингу;
- суміжні: GTM Lead, VP Marketing.

## 2. Архітектура

```
cron/launchd ─▶ jobradar run                         (jobradar/runner.py)
  0. feedback      getUpdates → натискання ✅/⏭ → vacancies.feedback
  1. collect       collectors/*  → [Vacancy]          (кожне джерело ізольоване)
  2. seen?         SQLite UNIQUE(source, source_id)   → старі пропускаємо
  3. title filter  pipeline/title_match.py            → title_rejected
  4. enrich        collector.enrich(v)                (повний опис, компанія, ЗП, remote)
  5. rules         pipeline/rules.py                  → rule_rejected
  6. dedup         pipeline/dedup.py (компанія+назва) → duplicate
  7. score         pipeline/scorer.py (Gemini | Ollama | Claude | евристика)
                   → scored | error (до 3 спроб); 429 від провайдера → зупинка, решта чекає
  8. notify        notify/telegram.py: apply → окремі повідомлення з кнопками,
                   maybe → один дайджест, решта → тихо позначається як оброблена (sent)
  9. alert         якщо джерело зламалося → ⚠️ у Telegram (не частіше 1 разу на добу)
```

Статуси в `vacancies.status`: `title_rejected`, `rule_rejected`, `duplicate`, `new`
(очікує скорингу), `error`, `scored` (очікує надсилання), `sent`.
Поле `feedback` має значення `applied` або `skipped` і заповнюється кнопками в Telegram.
Останні реакції потрапляють у промпт LLM як калібрування (`scoring.feedback_examples`).

## 3. Ключові рішення

| Рішення | Чому |
|---|---|
| Python 3.12 + uv, SQLite, без фреймворків | Мінімум залежностей; запуск з cron/launchd на ноутбуці чи VPS. |
| Djinni читається через **RSS** | Офіційний публічний фід із повним описом, без парсингу HTML. |
| Work.ua і Robota.ua читаються через **e-mail-розсилки (IMAP)** | Обидва сайти віддають не-браузерним клієнтам Cloudflare JS-challenge (403). Обходити захист від ботів ми **свідомо не будемо**. Розсилки ж є легальним офіційним каналом. Деталі в §4. |
| Двоступеневий фільтр: regex по назві → LLM | Пошук на сайтах дає багато шуму (~80% відсіюється назвою), а LLM викликається лише для релевантного. |
| Кожна вакансія оцінюється LLM лише раз | Стан зберігається в SQLite, тому повторні запуски дешеві. |
| **Пріоритет безкоштовних інструментів** (рішення власника) | `scoring.provider: auto` обирає Gemini (безкоштовний тариф), якщо є `GEMINI_API_KEY`; інакше Claude, якщо є ключ Anthropic; інакше евристику. Ollama вмикається явно. |
| Gemini: REST `generateContent`, `gemini-3.5-flash-lite`, `responseMimeType` + `responseJsonSchema` | На безкоштовному тарифі модель «Free of charge», і Google радить її для дешевих масових задач. Схема JSON примусова. Якщо API відхилить `responseJsonSchema`, скорер сам переходить у простий JSON-режим (`_schema_mode`). Між запитами пауза 6 с (`gemini.min_interval_sec`). `generateContent` у документації названо legacy, але він підтримується; рекомендованим є Interactions API. |
| Ollama: `/api/chat`, `format` = JSON-схема, `gemma3:4b` (3.4 ГБ) | Повністю локально і безкоштовно. Маленька модель обрана, бо диск власника заповнений на 96% (вільно ~21 ГБ). Кращі варіанти: `qwen3:8b` (5.2 ГБ), `gemma3:12b` (8.2 ГБ). |
| Claude `claude-opus-5-5`, effort `medium`, structured output (JSON schema) | Платний варіант. Строгий JSON; `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`) перезапускає запит на іншій моделі, якщо модель відмовила. Налаштування в розділі `claude:` конфігу. |
| Спільний промпт, схема і `result_from_data` для всіх LLM | Однакова логіка вердикту: пороги з конфігу, а «skip» від моделі (стоп-фактор) завжди перемагає. |
| `RateLimited` (HTTP 429) зупиняє скоринг на цей прогін | На безкоштовному тарифі ліміт може закінчитися посеред прогону. Вакансії лишаються в `new` без витрачання спроб. |
| Евристичний скоринг як fallback | Без жодного LLM або з `--no-llm` пайплайн усе одно працює (збіг ключових слів профілю). |
| Telegram через сирий Bot API (`httpx`) | Бібліотека не потрібна. Кнопки читаються через `getUpdates` на початку кожного прогону, тож постійно запущений бот-процес теж не потрібен. |
| `--dry-run` нічого не «споживає» | Оцінені в dry-run вакансії підуть у Telegram при першому бойовому запуску. |
| Чесний User-Agent і пауза ≥2 с між запитами до одного хоста | Ввічливість; robots.txt Djinni дозволяє `/jobs/`. |
| `profile.yaml`, `.env`, `data/` у `.gitignore` | Репозиторій публічний. |

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
- Живий прогін: 16 пошуків → 197 унікальних вакансій → 39 пройшли фільтр назви → 16 відсіяли
  правила (11 iGaming, 4 старші за 21 день) → 23 оцінено. Триває ~2 хв через паузи.

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
- HTML лінеаризується в послідовність текстових фрагментів і посилань. Посилання на вакансію визначається регекспом
  (зокрема після url-decode трекінгових редиректів). Назва береться з тексту першого
  «змістовного» посилання; кнопки на кшталт «Детальніше» чи «Відгукнутися» та картинки пропускаються.
  Сніпет — це текст до наступної вакансії (компанія, місто, ЗП). Зарплата парситься регекспом.
- ⚠ **Перевірено лише на синтетичних листах** (`tests/fixtures/email_*.eml`). Справжня верстка
  листів невідома. Після першого реального листа його треба зберегти як фікстуру (без
  персональних даних) і підправити парсер, якщо він помиляється.
- Повного опису немає, тому `description_is_snippet=True`, і LLM про це знає.

## 5. Структура репозиторію

```
BIBLE.md                 цей файл
README.md                швидкий старт
SETUP.md                 покрокове налаштування для власника-початківця
config.yaml              пошуки, фільтри, пороги, модель (без секретів)
profile.example.yaml     шаблон профілю → копія в profile.yaml (git-ignored)
.env.example             ключі → копія в .env (git-ignored)
jobradar/
  cli.py                 команди: run, feedback, tg-setup, tg-test, stats, show, rescore, llm-check
  runner.py              оркестрація прогону (RunReport, process_feedback)
  config.py              завантаження config/profile/.env → Settings
  db.py                  SQLite: vacancies + meta (tg_offset, last_run, last_collector_alert)
  models.py              Vacancy, ScoreResult
  http.py                PoliteClient: rate limit на хост, retries, чесний UA
  text.py                html_to_text, normalize
  collectors/base.py     Collector (collect, enrich), CollectorError
  collectors/djinni.py   RSS + JSON-LD
  collectors/email_alerts.py  Work.ua/Robota.ua з IMAP
  pipeline/title_match.py     роль+домен у вікні N слів, точні C-level, виключення
  pipeline/rules.py      стоп-слова, мін. ЗП (USD), макс. вік
  pipeline/dedup.py      ключ «компанія|назва» для крос-сайтового дедупу
  pipeline/scorer.py     GeminiScorer (REST), OllamaScorer, LLMScorer (Claude, Anthropic SDK), HeuristicScorer
  notify/telegram.py     форматування (HTML), кнопки, getUpdates
scripts/run.sh           один прогін із логом (для cron/launchd)
scripts/com.jobradar.run.plist.template   launchd, кожні 2 год
tests/                   pytest; fixtures/ — урізаний реальний RSS Djinni, JSON-LD, синтетичні листи
```

## 6. Конфігурація і секрети

- `.env`: `GEMINI_API_KEY` (або `GOOGLE_API_KEY`), `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `IMAP_HOST`,
  `IMAP_USER`, `IMAP_PASSWORD` (для Gmail потрібен *пароль застосунку*), опційно `JOBRADAR_DB`.
- `config.yaml`, найважливіше:
  - `djinni.searches`: список параметрів RSS;
  - `title_filter.exclude`: агресивні виключення (performance, smm, crm, retention, affiliate…).
    Через них «Chief Marketing/Retention Officer» теж відсіюється. Це свідомо, але правиться тут;
  - `rules.exclude_keywords`: за замовчуванням `gambling, igaming, casino, betting`
    (⚠ припущення про вподобання власника, треба підтвердити);
  - `scoring.provider`: `auto | gemini | ollama | claude | heuristic`; налаштування кожного
    провайдера в однойменних розділах `gemini:`, `ollama:` і `claude:`;
  - `scoring.max_per_run: 25` обмежує кількість LLM-викликів за прогін;
  - `notify.min_score`, `scoring.thresholds` (apply ≥80, maybe ≥55).
- `profile.yaml` повністю потрапляє в системний промпт. Блок `keywords` використовується тільки евристикою.
  Поточний профіль власника складено з paster.pro. `preferences` і `deal_breakers` позначені ⚠
  як припущення.

## 7. Як запускати і тестувати

Покрокова інструкція для власника (початківця): **[SETUP.md](SETUP.md)**.

```bash
uv sync
uv run pytest                                   # 75 тестів, офлайн
uv run jobradar llm-check                       # один виклик LLM на тестовій вакансії
uv run jobradar --db /tmp/t.sqlite3 run --dry-run --no-llm   # живий Djinni, без витрат
uv run jobradar tg-setup && uv run jobradar tg-test          # після налаштування бота
uv run jobradar run                              # бойовий прогін
```

- `tests/test_llm_sdk.py` проганяє `LLMScorer` через **справжній** Anthropic SDK з
  підміненим транспортом і перевіряє тіло запиту (fallbacks, output_config, cache_control).
  Нюанс: SDK 2.x працює на `httpx2`, а не на `httpx`, тож моки треба робити через `httpx2.MockTransport`.
- Розклад: `scripts/run.sh` + launchd-шаблон (див. README). macOS виконує пропущений запуск після пробудження.
  GitHub Actions не обрано: між запусками губиться SQLite, а IP датацентрів частіше блокують.

## 8. Вартість

**Типова конфігурація безкоштовна:** Djinni RSS, Telegram, Gmail IMAP, launchd на Mac,
Gemini на безкоштовному тарифі або Ollama локально.
- Gemini free: ліміти встановлюються на проєкт і публікуються лише в AI Studio
  (https://aistudio.google.com/rate-limit). За даними розробників на осінь 2026 року, для Flash-Lite
  це приблизно 15 RPM і 500 RPD, а для Flash лише ~20 RPD. Нам треба ~25 запитів у перший день,
  а далі кілька на день. На безкоштовному тарифі Google використовує контент для покращення
  продуктів. У промпт потрапляють публічні вакансії й профіль із публічного сайту.
- Ollama: $0, обмеження лише на диск і швидкість ноутбука.

Платний варіант (Claude): на старті Djinni дає ~20–25 релевантних вакансій, далі ~2–6 нових на день.
Один скоринг — це ~3–5K вхідних і ~1–2K вихідних токенів (разом із thinking) на `claude-opus-5-5`
($4 / $20 за 1M), тобто приблизно $0.03–0.06 за вакансію і кілька доларів на місяць (оцінка,
не вимір: після першого бойового прогону треба звірити з usage в консолі Anthropic).
Здешевити можна через `scoring.model` (`claude-sonnet-5-5`, `claude-haiku-5-5`) або effort `low`.

## 9. Статус і наступні кроки

Зроблено (2026-11-13):
- [x] Дослідження трьох сайтів (§4)
- [x] Колектор Djinni (RSS + JSON-LD) — перевірено вживу
- [x] Колектор e-mail-розсилок Work.ua/Robota.ua — перевірено на синтетичних листах
- [x] SQLite, дедуп у межах сайту і між сайтами, повторні спроби скорингу (до 3)
- [x] Фільтр назв (перевірено на 197 живих назвах Djinni) і правила
- [x] LLM-скоринг (Claude, structured output, fallbacks) і евристичний fallback
- [x] Telegram: повідомлення, дайджест, кнопки ✅/⏭ → feedback → калібрування промпту
- [x] Алерт у Telegram, коли джерело зламалося
- [x] CLI, launchd-шаблон, 66 тестів

- [x] Безкоштовні провайдери: Gemini (безкоштовний тариф) і Ollama, команда `llm-check`, SETUP.md для початківця

Не перевірено вживу (бракує ключів на машині розробки):
- [x] Реальний виклик Gemini (2026-11-13): `llm-check` → `gemini-3.5-flash-lite`, безкоштовний тариф,
      `responseJsonSchema` приймається (fallback не знадобився). Помічено: `matched` модель інколи пише
      англійською, а `cover_hook` виходить загальним, без конкретного кейсу. Якщо на реальних
      вакансіях це заважатиме, спробувати `gemini-3.8-flash` (але там менший денний ліміт) або підсилити промпт.
- [ ] Реальний виклик Ollama: Ollama на машині не встановлено
- [ ] Реальний виклик Claude: потрібен `ANTHROPIC_API_KEY`, після чого `jobradar rescore <id>`
- [ ] Реальне надсилання в Telegram: потрібні токен бота і `tg-setup`
- [ ] Реальні листи Work.ua/Robota.ua: потрібні підписки, IMAP і калібрування парсера

Наступні кроки:
1. Власник: пройти SETUP.md (Gemini або Ollama, Telegram), підтвердити ⚠-припущення в
   `profile.yaml` і `rules.exclude_keywords`.
2. Перший бойовий прогін, перевірити якість оцінок LLM на 10–20 вакансіях, підкрутити промпт і пороги.
3. Налаштувати підписки на work.ua і robota.ua (запити: «маркетинг директор», «head of marketing»,
   «бренд директор», «стратегічний директор», «CMO»), увімкнути `email_alerts.enabled`.
4. Зберегти реальний лист кожного сайту як фікстуру й відкалібрувати парсер.
5. (Опційно, за рішенням власника) колектор robota.ua через `dracula.robota.ua`, див. ризики в §4.
6. Ідеї: щотижневий звіт зі статистикою відгуків; команда «згенеруй супровідний лист» з кнопки.
