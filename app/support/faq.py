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
        key="phone",
        button="📞 Как посмотреть номер?",
        phrases=(
            "как получить номер",
            "посмотреть номер",
            "номер хозяина",
            "номер собственника",
            "контакт хозяина",
            "контакт собственника",
            "контакт по объявлению",
        ),
        answer=(
            "📞 Откройте нужную квартиру и нажмите «Получить номер». Если доступ "
            "оплачен, контакт по объявлению появится в этом же окне."
        ),
    ),
    FAQItem(
        key="week",
        button="💳 Сколько стоит доступ?",
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
            "отправить чек",
            "куда чек",
            "я оплатил",
            "оплата",
            "чек",
        ),
        answer=(
            "💳 Нажмите «Посмотреть номер», выберите тариф и оплатите по открывшейся "
            "ссылке. После оплаты дождитесь подтверждения."
        ),
    ),
    FAQItem(
        key="review",
        button="❓ Я оплатил, но доступа нет",
        phrases=(
            "сколько ждать",
            "проверка оплаты",
            "проверяют оплату",
            "чек проверяется",
            "не подтвердили",
            "когда подтвердят",
        ),
        answer=(
            "❓ Оплата проверяется вручную. Повторно не платите. После подтверждения "
            "нажмите «Посмотреть номер». Если подтверждения долго нет, напишите сюда "
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
        key="problem",
        button="🛠 Что делать, если кнопка не работает?",
        phrases=(
            "кнопка не работает",
            "ссылка недействительна",
            "ссылка не работает",
            "ошибка кнопки",
            "не открывается бот",
        ),
        answer=(
            "🛠 Закройте помощь, вернитесь к квартире и нажмите кнопку ещё раз. "
            "Если объявление уже снято, выберите другую свежую квартиру в группе."
        ),
    ),
    FAQItem(
        key="availability",
        button="🏠 Квартира ещё свободна?",
        phrases=(
            "квартира актуальна",
            "еще сдается",
            "уже сдали",
            "объявление актуально",
            "квартира доступна",
        ),
        answer=(
            "🏠 Уточните это по полученному номеру: квартиру могли сдать после "
            "публикации объявления."
        ),
    ),
    FAQItem(
        key="catalog",
        button="🏙 Где посмотреть квартиры?",
        phrases=(
            "какие квартиры",
            "где каталог",
            "покажи квартиры",
            "районы",
            "цены квартир",
            "двушки",
            "однушки",
            "студии",
        ),
        answer=(
            "🏙 Все свежие квартиры публикуются в Telegram-группе. Выберите карточку "
            "и нажмите «Посмотреть номер»."
        ),
    ),
    FAQItem(
        key="safety",
        button="🛡 Как снять квартиру безопасно?",
        phrases=(
            "это мошенники",
            "безопасно",
            "скам",
            "предоплата хозяину",
            "как не обманут",
        ),
        answer=(
            "🛡 Не переводите задаток до просмотра квартиры и проверки документов. "
            "Никому не сообщайте коды из SMS."
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
    FAQItem(
        key="privacy",
        button="🔒 Кто увидит номер?",
        phrases=(
            "персональные данные",
            "кто увидит номер",
            "виден ли номер",
            "безопасность данных",
            "мой чек",
        ),
        answer=(
            "🔒 Номер показывается только вам в окне доступа. В общей группе он не "
            "публикуется."
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
        "Я отвечаю по работе сервиса: квартиры и их актуальность, получение "
        "номера, тарифы, оплата, заявка на поиск, безопасность "
        "и ошибки кнопок. Выберите подходящий раздел кнопкой ниже."
    )
