# USOUSDT Bybit — Telegram-бот аналітики

Особистий Telegram-бот, який аналізує безстроковий контракт **USOUSDT** на Bybit
(таймфрейм 5 хв) і надсилає розбір із ймовірністю лонгу/шорту. Бот **лише читає**
публічні ринкові дані Bybit і нічого не торгує.

## Стан розробки

| Етап | Що | Стан |
| --- | --- | --- |
| 1 | Каркас, конфіг | ✅ |
| 2 | Дані Bybit: REST, WebSocket, кити, індикатори | ⏳ |
| 3 | Новини і календар | ⏳ |
| 4 | Аналіз моделлю | ⏳ |
| 5 | Telegram: кнопка і команди | ⏳ |
| 6 | Збереження прогнозів, `/stats`, `/export` | ⏳ |
| 7 | Автосповіщення | ⏳ |
| 8 | Деплой | ⏳ |

## Встановлення

Потрібен Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # і заповнити значення
```

### `.env`

| Змінна | Що це |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | токен від @BotFather |
| `TELEGRAM_OWNER_ID` | ваш числовий Telegram ID (бот відповідає лише вам) |
| `ANTHROPIC_API_KEY` | ключ Anthropic API |
| `ANTHROPIC_MODEL` | модель для аналізу, напр. `claude-sonnet-5-5` |

### `config.yaml`

Усе інше: символ, комісія, пороги китів і тригерів, чутливість, паузи, ліміт
запитів до моделі, RSS-стрічки, ключові слова, календар подій, часовий пояс показу.
Невідомий ключ у файлі вважається помилкою (захист від одруківок).

## Запуск

```bash
python -m bot.main --check-config   # лише перевірити конфіг
python -m bot.main                  # запуск бота
pytest                              # тести
```

## Структура

```text
bot/
├── main.py            # запуск
├── config.py          # .env + config.yaml
├── logging_setup.py   # лог із ротацією, маскування секретів
├── telegram/          # хендлери, клавіатура, owner-middleware, форматування
├── market/            # Bybit REST/WS, кити, індикатори
├── news/              # RSS, календар подій
└── tracking/          # SQLite, перевірка прогнозів, статистика
prompts/analysis.md    # промпт для моделі
tests/
```
