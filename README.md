# work-search · jobradar

Відстежує вакансії рівня Head / Director / C-level у маркетингу, бренді та стратегії на
**Djinni**, **Work.ua**, **Robota.ua**, оцінює їх щодо вашого профілю (Claude) і надсилає
в Telegram ті, на які варто відгукнутися.

- **Покрокове налаштування для початківця (усе безкоштовно): [SETUP.md](SETUP.md)**
- Повний опис архітектури, рішень і статусу: [BIBLE.md](BIBLE.md)

Оцінка вакансій (`scoring.provider` у `config.yaml`): **Gemini** (безкоштовний тариф),
**Ollama** (локально, безкоштовно), **Claude** (платно) або евристика за ключовими словами.

## Швидкий старт

```bash
# 1. залежності (потрібен uv: https://docs.astral.sh/uv/)
uv sync

# 2. налаштування
cp .env.example .env                    # ключі: Gemini (або Ollama/Claude), Telegram, (опц.) IMAP
cp profile.example.yaml profile.yaml    # ваш профіль — його читає LLM

# 3. перевірка без Telegram і без LLM
uv run jobradar run --dry-run --no-llm

# 4. Telegram: створіть бота в @BotFather, напишіть йому будь-що, потім
uv run jobradar tg-setup                # покаже TELEGRAM_CHAT_ID → впишіть у .env
uv run jobradar tg-test

# 5. бойовий запуск
uv run jobradar run
```

## Команди

| Команда | Що робить |
|---|---|
| `jobradar run` | зібрати → відфільтрувати → оцінити → надіслати |
| `jobradar run --dry-run` | те саме, але друкує в консоль; нічого не позначає як надіслане |
| `jobradar run --no-llm` | евристичний скоринг замість LLM |
| `jobradar llm-check` | оцінити тестову вакансію: перевірка ключа, моделі чи Ollama |
| `jobradar run --sources djinni` | лише вибрані джерела (`djinni`, `email`) |
| `jobradar feedback` | забрати натискання кнопок ✅/⏭ з Telegram |
| `jobradar stats` | кількість вакансій за джерелом і статусом |
| `jobradar show --status sent` | список вакансій |
| `jobradar rescore 123` | переоцінити одну вакансію й показати результат |

## Розклад

`scripts/run.sh` — один прогін із логом у `data/logs/`. На macOS:

```bash
sed "s#__REPO__#$PWD#g" scripts/com.jobradar.run.plist.template > ~/Library/LaunchAgents/com.jobradar.run.plist
launchctl load ~/Library/LaunchAgents/com.jobradar.run.plist
```

## Тести

```bash
uv run pytest
```
