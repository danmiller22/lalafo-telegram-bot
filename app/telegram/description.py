"""Small, local public-card summaries with contact information removed."""
from __future__ import annotations

from html import unescape
import re


SUMMARY_LIMIT = 160
SENTENCE_LIMIT = 80
_URL = re.compile(
    r"(?:https?://|www\.|tel:|mailto:)\S+|"
    r"\b(?:t\.me|wa\.me|api\.whatsapp\.com|telegram\.me)/\S+|"
    r"[\w.+-]+@[\w.-]+\.[a-z]{2,}|@[\w]{3,}", re.IGNORECASE,
)
_NUMBER = re.compile(r"\+?[\dOoОо][\dOoОо\s()./–—_\-]*[\dOoОо]")
_CONTACT = re.compile(
    r"📞|☎|📱|\b(?:телефон\w*|тел\.|номер\w*|контакт\w*|"
    r"whats?app|ватс?ап\w*|вацап\w*|telegram|телеграм\w*|"
    r"звоните|звонить|пишите|писать|обращайтесь)\b", re.IGNORECASE,
)


def short_public_description(value: str | None, *, phone: str = "") -> str:
    """Use source facts only; never infer amenities or expose a contact route."""
    text = unescape(value or "")
    text = re.sub(r"<[^>]*>", " ", text)
    text = _URL.sub("", text)
    known = re.sub(r"\D", "", phone)
    if len(known) >= 7:
        text = re.sub(r"[\s()./–—_\-]*".join(map(re.escape, known)), "", text)
    lines = []
    for line in text.splitlines():
        line = _NUMBER.sub(
            lambda m: "" if sum(c.isdigit() or c in 'OoОо' for c in m[0]) >= 7 else m[0],
            line,
        )
        # Contact instructions are usually at the end of a sentence/line.
        line = _CONTACT.split(line, maxsplit=1)[0]
        line = re.sub(r"\s+", " ", line).strip(" \t-–—•,;:|📍💰🏠")
        if not line or not re.search(r"[а-яa-z]", line, re.IGNORECASE):
            continue
        if line.casefold().strip('.!:') in {'описание', 'сдаётся квартира', 'сдается квартира'}:
            continue
        if re.search(r"ищете квартир|подайте заявк|подпиш|переходите|наш\w* канал", line, re.IGNORECASE):
            continue
        if line not in lines:
            lines.append(line)
    sentences = []
    for line in lines:
        # A decimal point inside a price or area is not a sentence boundary.
        for sentence in re.split(r"(?<=[.!?…])\s+(?!\d)|(?<=[!?…])(?=\S)", line):
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) > SENTENCE_LIMIT:
                sentence = sentence[:SENTENCE_LIMIT - 1].rsplit(' ', 1)[0].rstrip(' ,;:-') + '…'
            sentences.append(sentence)
            if len(sentences) == 2:
                break
        if len(sentences) == 2:
            break
    result = " ".join(sentences)
    if len(result) > SUMMARY_LIMIT:
        result = result[:SUMMARY_LIMIT - 1].rsplit(' ', 1)[0].rstrip(' ,;:-') + '…'
    return result or "Подробные условия аренды уточняйте при обращении."
