from __future__ import annotations

import re
from dataclasses import dataclass

from app.payment_plans import WEEK_PRICE


@dataclass(frozen=True, slots=True)
class FAQItem:
    key: str
    button: str
    phrases: tuple[str, ...]
    answer: str


FAQ_ITEMS = (
    FAQItem(
        key="phone",
        button="📞 Как получить номер",
        phrases=(
            "как получить номер",
            "посмотреть номер",
            "номер хозяина",
            "номер собственника",
            "контакт хозяина",
            "контакт собственника",
        ),
        answer=(
            "📞 Откройте нужную квартиру и нажмите «Посмотреть номер». После "
            "активации недельного доступа бот пришлёт полную карточку с номером "
            "сюда, в личный чат. Другие пользователи номер не увидят."
        ),
    ),
    FAQItem(
        key="week",
        button="⭐ Недельный доступ",
        phrases=(
            "недельный доступ",
            "доступ на неделю",
            "сколько стоит доступ",
            "цена доступа",
            "тариф",
            "500 сом",
        ),
        answer=(
            f"⭐ Недельный доступ стоит {WEEK_PRICE} сом и работает ровно 7 дней "
            "с момента подтверждения. В течение недели можно получать номера "
            "под любыми доступными карточками без повторной оплаты."
        ),
    ),
    FAQItem(
        key="payment",
        button="💳 Оплата и чек",
        phrases=(
            "как оплатить",
            "куда оплатить",
            "ссылка на оплату",
            "отправить чек",
            "куда чек",
            "я оплатил",
            "оплата",
            "чек",
        ),
        answer=(
            "💳 Нажмите «Посмотреть номер», выберите недельный доступ, откройте "
            "ссылку на оплату и затем отправьте этому боту фото или файл чека. "
            "Без чека заявка на проверку не создаётся."
        ),
    ),
    FAQItem(
        key="review",
        button="⏳ Проверка оплаты",
        phrases=(
            "сколько ждать",
            "проверка оплаты",
            "проверяют оплату",
            "чек проверяется",
            "не подтвердили",
            "когда подтвердят",
        ),
        answer=(
            "⏳ Если бот написал «Чек получен», повторно платить не нужно. "
            "После проверки вам автоматически придёт подтверждение и карточка "
            "квартиры с номером."
        ),
    ),
    FAQItem(
        key="wanted",
        button="🔎 Заявка на поиск",
        phrases=(
            "подать заявку",
            "заявка на поиск",
            "ищу квартиру",
            "найти квартиру",
            "разместить заявку",
        ),
        answer=(
            "🔎 В главном меню нажмите «Разместить „Ищу квартиру“», заполните "
            "короткую анкету и оплатите 100 сом. После подтверждения заявка будет "
            "опубликована в группе."
        ),
    ),
    FAQItem(
        key="problem",
        button="🛠 Кнопка не работает",
        phrases=(
            "кнопка не работает",
            "ссылка недействительна",
            "ссылка не работает",
            "ошибка кнопки",
            "не открывается бот",
        ),
        answer=(
            "🛠 Откройте личный чат бота, нажмите /start и снова откройте кнопку "
            "под нужной квартирой. Если ошибка останется, напишите номер или "
            "пришлите ссылку на квартиру — вопрос будет передан администратору."
        ),
    ),
)

FAQ_BY_KEY = {item.key: item for item in FAQ_ITEMS}


def normalize_question(text: str) -> str:
    normalized = text.casefold().replace("ё", "е")
    normalized = re.sub(r"[^a-zа-я0-9]+", " ", normalized)
    return " ".join(normalized.split())


def faq_for_text(text: str) -> FAQItem | None:
    """Return an answer only for a confident phrase match.

    Unknown or vague messages deliberately fall through to the administrator.
    """
    normalized = normalize_question(text)
    if not normalized:
        return None
    matches: list[tuple[int, FAQItem]] = []
    for item in FAQ_ITEMS:
        for phrase in item.phrases:
            normalized_phrase = normalize_question(phrase)
            if normalized_phrase in normalized:
                matches.append((len(normalized_phrase), item))
    return max(matches, key=lambda pair: pair[0])[1] if matches else None
