from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def moderator_panel_kb(publishing_enabled: bool = False) -> InlineKeyboardMarkup:
    """Главная панель модератора. Тумблер публикации отображает
    текущее состояние publishing_enabled из таблицы settings."""
    toggle_text = (
        "🟢 Публикация: ВКЛ" if publishing_enabled else "🔴 Публикация: ВЫКЛ"
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_text, callback_data="toggle_publishing")],
        [InlineKeyboardButton(text="📥 Загрузить задачи", callback_data="load_tasks")],
        [InlineKeyboardButton(text="🛑 Остановить / очистить очередь", callback_data="clear_queue")],
        [InlineKeyboardButton(text="🔄 Обновить расписание", callback_data="refresh_schedule")],
    ])


def publish_task_kb(record_id: str) -> InlineKeyboardMarkup:
    """Кнопка [✅ Опубликовать] для задачи в панели модератора."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"publish_{record_id}")],
    ])


def test_take_task_kb(record_id: str) -> InlineKeyboardMarkup:
    """Кнопка [👤 Взять задачу (тест)] для TEST_MODE."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Взять задачу (тест)", callback_data=f"take_test_{record_id}")],
    ])


def build_publish_keyboard(record_id: str, test_mode: bool) -> InlineKeyboardMarkup | None:
    """Клавиатура для сообщения, опубликованного в рабочую группу.

    В TEST_MODE публикация имитируется в MODERATOR_GROUP_ID, поэтому нужна
    кнопка «Взять задачу (тест)». В PROD исполнители берут задачу реакцией —
    клавиатура не нужна.
    """
    if test_mode:
        return test_take_task_kb(record_id)
    return None


def accept_reject_kb(record_id: str, user_id: int) -> InlineKeyboardMarkup:
    """Кнопки для карточки результата в группе модераторов."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Принять", callback_data=f"accept_{record_id}_{user_id}"),
            InlineKeyboardButton(text="❌ Не принимать", callback_data=f"reject_{record_id}_{user_id}"),
        ],
    ])


def stale_notification_kb(record_id: str) -> InlineKeyboardMarkup:
    """Кнопки для уведомления о простое."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔁 Да, опубликовать повторно", callback_data=f"restale_{record_id}"),
            InlineKeyboardButton(text="➡️ Оставить", callback_data=f"skip_stale_{record_id}"),
        ],
    ])
