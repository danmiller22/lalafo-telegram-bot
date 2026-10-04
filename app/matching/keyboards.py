from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


PAGE_SIZE = 8


def hot_start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🏠 Смотреть все",
                    callback_data="matching:start",
                )
            ]
        ]
    )


def rooms_keyboard(selected: set[str]) -> InlineKeyboardMarkup:
    def label(value: str, text: str) -> str:
        return f"✅ {text}" if value in selected else text

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=label("studio", "Студия"),
                    callback_data="matching:room:studio",
                ),
                InlineKeyboardButton(
                    text=label("1", "1-комнатная"),
                    callback_data="matching:room:1",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Далее →",
                    callback_data="matching:rooms:done",
                )
            ],
        ]
    )


def districts_keyboard(
    options: list[str], selected: set[str], *, page: int, all_districts: bool
) -> InlineKeyboardMarkup:
    page_count = max(1, (len(options) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, page_count - 1))
    start = page * PAGE_SIZE
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=("✅ " if all_districts else "") + "Весь Бишкек",
                callback_data="matching:district:all",
            )
        ]
    ]
    for index in range(start, min(start + PAGE_SIZE, len(options))):
        value = options[index]
        rows.append(
            [
                InlineKeyboardButton(
                    text=("✅ " if value in selected else "") + value,
                    callback_data=f"matching:district:{index}",
                )
            ]
        )
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(text="← Назад", callback_data=f"matching:page:{page - 1}")
        )
    navigation.append(
        InlineKeyboardButton(text=f"{page + 1}/{page_count}", callback_data="matching:noop")
    )
    if page + 1 < page_count:
        navigation.append(
            InlineKeyboardButton(text="Далее →", callback_data=f"matching:page:{page + 1}")
        )
    rows.append(navigation)
    rows.append(
        [InlineKeyboardButton(text="Готово", callback_data="matching:districts:done")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def budget_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="23 000", callback_data="matching:budget:23000"),
                InlineKeyboardButton(text="25 000", callback_data="matching:budget:25000"),
            ],
            [
                InlineKeyboardButton(text="30 000", callback_data="matching:budget:30000"),
                InlineKeyboardButton(text="35 000", callback_data="matching:budget:35000"),
            ],
            [InlineKeyboardButton(text="40 000", callback_data="matching:budget:40000")],
            [InlineKeyboardButton(text="Указать свой", callback_data="matching:budget:custom")],
        ]
    )


def feed_footer_keyboard(*, has_more: bool = True) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if has_more:
        rows.append(
            [InlineKeyboardButton(text="Показать ещё", callback_data="matching:more")]
        )
    rows.extend(
        [
            [InlineKeyboardButton(text="⚙️ Изменить фильтр", callback_data="matching:start")],
            [
                InlineKeyboardButton(
                    text="🔔 Приостановить уведомления",
                    callback_data="matching:toggle",
                )
            ],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
