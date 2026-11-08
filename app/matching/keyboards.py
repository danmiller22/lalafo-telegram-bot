from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


PAGE_SIZE = 8

DISTRICT_PRESETS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "center",
        "🏙 Центр",
        (
            "центр",
            "золотой квадрат",
            "цум",
            "гум",
            "филармон",
            "моссовет",
            "медакадем",
            "ошский рынок",
        ),
    ),
    (
        "upper",
        "⛰ Верхние районы",
        (
            "асанбай",
            "южная магистраль",
            "орто-сай",
            "ататюрк",
            "ботанический сад",
            "верхний джал",
            "юг-2",
        ),
    ),
    ("micro", "🏢 Микрорайоны", ("мкр", "микрорайон")),
    ("jal", "🏠 Джал", ("джал", "политех")),
    ("east", "🌅 Восточные районы", ("восток", "тунгуч", "аламедин")),
)


def district_preset_members(options: list[str], key: str) -> set[str]:
    preset = next((item for item in DISTRICT_PRESETS if item[0] == key), None)
    if preset is None:
        return set()
    needles = preset[2]
    return {
        option
        for option in options
        if any(needle in option.casefold().replace("ё", "е") for needle in needles)
    }


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
            [InlineKeyboardButton(
                text=label("2", "2-комнатная"), callback_data="matching:room:2"
            )],
            [
                InlineKeyboardButton(
                    text="Далее →",
                    callback_data="matching:rooms:done",
                )
            ],
        ]
    )


def seller_types_keyboard(selected: set[str]) -> InlineKeyboardMarkup:
    def label(value: str, text: str) -> str:
        return f"✅ {text}" if value in selected else text

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=label("owner", "👤 От собственников"),
                    callback_data="matching:seller:owner",
                )
            ],
            [
                InlineKeyboardButton(
                    text=label("realtor", "🏢 От агентств недвижимости"),
                    callback_data="matching:seller:realtor",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Далее →",
                    callback_data="matching:sellers:done",
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
    for key, title, _ in DISTRICT_PRESETS:
        members = district_preset_members(options, key)
        active = bool(members) and members.issubset(selected)
        rows.append(
            [
                InlineKeyboardButton(
                    text=("✅ " if active else "") + title,
                    callback_data=f"matching:preset:{key}",
                )
            ]
        )
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
                InlineKeyboardButton(text="до 25 000 сом", callback_data="matching:budget:25000"),
                InlineKeyboardButton(text="до 30 000 сом", callback_data="matching:budget:30000"),
            ],
            [
                InlineKeyboardButton(text="до 35 000 сом", callback_data="matching:budget:35000"),
                InlineKeyboardButton(text="до 40 000 сом", callback_data="matching:budget:40000"),
            ],
            [InlineKeyboardButton(text="до 50 000 сом", callback_data="matching:budget:50000")],
            [InlineKeyboardButton(text="до 60 000 сом", callback_data="matching:budget:60000")],
            [InlineKeyboardButton(text="Указать свой", callback_data="matching:budget:custom")],
        ]
    )


def feed_footer_keyboard(*, has_more: bool = True) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [[InlineKeyboardButton(
        text="✨ Под меня — 1 499 сом", callback_data="personal:offer"
    )]]
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
