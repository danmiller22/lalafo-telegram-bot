"""Use addresses explicitly present in source text, never invent a location."""
import re
STREETS=(r'Манаса|Токтогула|Белинка|Белинского|Уметалиева|Турусбекова|Гоголя|Огонбаева|'
 r'Чуй|Молодая\s+Гвардия|Молодой\s+Гвардии|Киевская|Московская|Советская|'
 r'Байтик\s+Баатыра|Ахунбаева|Тыныстанова|Жибек[ -]+Жолу|Фрунзе|Орозбекова|'
 r'Панфилова|Логвиненко|Шопокова|Суюмбаева|Абдрахманова|Калык[ -]+Акиева|'
 r'Рыскулова|Усенбаева|Абдымомунова|Коенкозова|Ибраимова|Боконбаева|Эркиндик|'
 r'Бакаева|Юнусалиева|Малдыбаева|Сыдыкова|Каралаева|Лермонтова|Исанова')
PAIR=re.compile(rf'\b({STREETS})\s*(?:/|\\|—|–|\s-\s|,?\s*пересечени[еи]\s*)\s*({STREETS})\b',re.I)
HOUSE=re.compile(rf'\b({STREETS})\s*,?\s+(\d{{1,3}}[А-Яа-яA-Za-z]?(?:/\d{{1,3}})?)(?!\d)',re.I)
GENERIC={'центр','центр города','бишкек','центральный район','город бишкек','центре','не указан'}

def clean(value):
    value=re.split(r'\b(?:телефон|тел|whatsapp|ватсап|цена|стоимость|оплата|депозит)\b',value,flags=re.I)[0]
    value=re.sub(r'(?<!\d)(?:\+?996[\s().-]*|0)[2-9](?:[\s().-]*\d){8}(?!\d)','',value)
    return value.strip(' .,;:—–-')[:120]

def source_location(district=None,description=None,title=None):
    text='\n'.join(str(v or '') for v in (title,description))
    addresses=[];areas=[]
    for line in text.splitlines():
        line=re.sub(r'^[^\w]+','',line).strip()
        match=re.match(r'^(адрес|дареги|локация|район|ориентир|микрорайон)\s*[:—–-]\s*(.+)',line,re.I)
        if match:
            value=clean(match[2])
            if value.casefold() not in GENERIC and len(value)>2:
                (addresses if match[1].casefold() in ('адрес','дареги') else areas).append(value)
    if addresses:return addresses[0]
    pair=PAIR.search(text)
    if pair:return f'{pair[1].strip()} / {pair[2].strip()}'
    house=HOUSE.search(text)
    if house:return f'{house[1].strip()}, {house[2]}'
    if areas:return areas[0]
    value=clean(str(district or ''))
    return value if value else None

def public_location(ad):
    return source_location(getattr(ad,'district',None),getattr(ad,'source_description',None),getattr(ad,'source_title',None)) or 'Район не указан'
