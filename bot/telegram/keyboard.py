from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

BTN_ANALYZE = "📊 Аналіз зараз"
BTN_STATUS = "ℹ️ Статус"
BTN_STATS = "📈 Статистика"
BTN_ALERTS_ON = "🔔 Сповіщення: увімк"
BTN_ALERTS_OFF = "🔕 Сповіщення: вимк"


def main_keyboard(alerts_enabled: bool) -> ReplyKeyboardMarkup:
    # Кнопка показує поточний стан; натискання перемикає його.
    toggle = BTN_ALERTS_ON if alerts_enabled else BTN_ALERTS_OFF
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_ANALYZE)], [KeyboardButton(text=BTN_STATUS),
                  KeyboardButton(text=BTN_STATS)], [KeyboardButton(text=toggle)]],
        resize_keyboard=True,
        is_persistent=True,
    )
