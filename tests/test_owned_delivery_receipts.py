import pytest
from tools.owned_delivery_receipts import batch_delivery_receipts

CARDS = [{'ad_id': 11, 'source_id': 101, 'snapshot': {'photo_urls': ['room']}},
         {'ad_id': 12, 'source_id': 102, 'snapshot': {'photo_urls': ['room', 'kitchen']}}]


def test_existing_and_new_messages_both_have_individual_receipts():
    logs = ('OWNED_CARD_ALREADY_PUBLISHED managed_id=11 published:501\n'
            'OWNED_CARD_PUBLISHED managed_id=12 source_id=102 message_id=502 photos=2\n')
    receipts, sent, already = batch_delivery_receipts(logs, CARDS)
    assert (sent, already) == (1, 1)
    assert [x['message_id'] for x in receipts] == [501, 502]
    assert [x['source_id'] for x in receipts] == [101, 102]


@pytest.mark.parametrize('bad', ['missing', 'source', 'message', 'duplicate', 'unknown'])
def test_incomplete_or_wrong_receipts_never_report_batch_success(bad):
    lines = ['OWNED_CARD_PUBLISHED managed_id=11 source_id=101 message_id=501 photos=1',
             'OWNED_CARD_PUBLISHED managed_id=12 source_id=102 message_id=502 photos=2']
    if bad == 'missing': lines.pop()
    elif bad == 'source': lines[1] = lines[1].replace('source_id=102', 'source_id=103')
    elif bad == 'message': lines[1] = lines[1].replace('message_id=502', 'message_id=501')
    elif bad == 'duplicate': lines.append(lines[0])
    else: lines[1] = lines[1].replace('managed_id=12', 'managed_id=13')
    with pytest.raises(ValueError):
        batch_delivery_receipts('\n'.join(lines), CARDS)
