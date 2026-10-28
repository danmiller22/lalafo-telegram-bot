"""Validate exact Telegram acknowledgements for an idempotent owned batch."""
import re


def batch_delivery_receipts(logs: str, cards: list[dict]) -> tuple[list[dict], int, int]:
    expected = {int(row['ad_id']): row for row in cards}
    if len(expected) != len(cards):
        raise ValueError('duplicate_manifest_ad')
    sent = re.findall(r'OWNED_CARD_PUBLISHED managed_id=(\d+) source_id=(-?\d+) message_id=(\d+) photos=(\d+)', logs)
    prior = re.findall(r'OWNED_CARD_ALREADY_PUBLISHED managed_id=(\d+) published:(\d+)', logs)
    receipts = {}
    for ad, source, message, photos in sent:
        aid = int(ad)
        if aid not in expected or aid in receipts or int(source) != int(expected[aid]['source_id']):
            raise ValueError('unexpected_telegram_delivery')
        receipts[aid] = {'managed_ad_id': aid, 'source_id': int(source),
                         'message_id': int(message), 'photos': int(photos), 'already_published': False}
    for ad, message in prior:
        aid = int(ad)
        if aid not in expected or aid in receipts:
            raise ValueError('unexpected_existing_telegram_delivery')
        receipts[aid] = {'managed_ad_id': aid, 'source_id': int(expected[aid]['source_id']),
                         'message_id': int(message), 'photos': len(expected[aid]['snapshot']['photo_urls']),
                         'already_published': True}
    if set(receipts) != set(expected):
        raise ValueError('telegram_receipts_incomplete')
    if len({x['message_id'] for x in receipts.values()}) != len(receipts):
        raise ValueError('telegram_message_reused_for_different_apartments')
    return [receipts[int(row['ad_id'])] for row in cards], len(sent), len(prior)
