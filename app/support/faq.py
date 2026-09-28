from __future__ import annotations

import re
from dataclasses import dataclass

from app.payment_plans import MONTH_PRICE, WANTED_SEARCH_PRICE, WEEK_PRICE


@dataclass(frozen=True, slots=True)
class FAQItem:
    key: str
    button: str
    phrases: tuple[str, ...]
    answer: str


FAQ_ITEMS = (
    FAQItem(
        key="week",
        button="💳 Тарифы",
        phrases=(
            "недельный доступ",
            "доступ на неделю",
            "сколько стоит доступ",
            "цена доступа",
            "тариф",
            f"{WEEK_PRICE} сом",
        ),
        answer=(
            f"💳 7 дней доступа к номерам — {WEEK_PRICE} сом.\n"
            f"30 дней доступа к номерам — {MONTH_PRICE} сом."
        ),
    ),
    FAQItem(
        key="payment",
        button="💳 Как оплатить?",
        phrases=(
            "как оплатить",
            "куда оплатить",
            "ссылка на оплату",
            "я оплатил",
            "оплата",
        ),
        answer=(
            "💳 Нажмите «Получить номер», ознакомьтесь с условиями, выберите тариф "
            "и оплатите по открывшейся ссылке. Оплата предоставляет доступ к "
            "контактам объявлений, а не гарантию аренды или актуальности квартиры. "
            "После оплаты нажмите «Я оплатил(а) — открыть номер»: номер появится "
            "сразу, а доступ ко всем карточкам сохранится на срок тарифа."
        ),
    ),
    FAQItem(
        key="review",
        button="❓ Оплатил, но номер не открылся",
        phrases=(
            "оплатил но номера нет",
            "не открылся номер",
            "не пришел доступ",
            "не пришёл доступ",
        ),
        answer=(
            "❓ Вернитесь к экрану оплаты и нажмите «Я оплатил(а) — открыть номер». Карточка с "
            "контактом выдаётся автоматически. Если она не появилась, напишите сюда "
            "сумму и время оплаты."
        ),
    ),
    FAQItem(
        key="wanted",
        button="🔎 Как подать заявку?",
        phrases=(
            "подать заявку",
            "заявка на поиск",
            "ищу квартиру",
            "найти квартиру",
            "разместить заявку",
        ),
        answer=(
            "🔎 В главном меню нажмите «Разместить „Ищу квартиру“», заполните "
            f"короткую анкету и оплатите {WANTED_SEARCH_PRICE} сом. После подтверждения заявка будет "
            "опубликована в группе."
        ),
    ),
    FAQItem(
        key="refund",
        button="↩️ Деньги списались дважды",
        phrases=(
            "деньги списались",
            "оплатил дважды",
            "ошибка оплаты",
            "возврат",
            "не пришел доступ",
            "не пришёл доступ",
        ),
        answer=(
            "↩️ Напишите сюда сумму и время каждого списания. Не отправляйте данные "
            "карты и коды из SMS."
        ),
    ),
)

FAQ_BY_KEY = {item.key: item for item in FAQ_ITEMS}


def normalize_question(text: str) -> str:
    normalized = text.casefold().replace("ё", "е")
    normalized = re.sub(r"[^a-zа-я0-9]+", " ", normalized)
    return " ".join(normalized.split())


def faq_for_text(text: str) -> FAQItem | None:
    """Return the most relevant deterministic self-service answer."""
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


def fallback_answer(text: str) -> str:
    """Answer unknown questions without creating a human-support ticket."""
    normalized = normalize_question(text)
    if normalized.startswith(("want", "mywanted")):
        return (
            "Все действия доступны кнопками. Закройте помощь и выберите "
            "«Подать заявку на поиск квартиры» или «Мои заявки» в главном меню."
        )
    return (
        "Я отвечаю по тарифам, оплате и заявкам на поиск квартиры. "
        "Выберите подходящий раздел кнопкой ниже."
    )
