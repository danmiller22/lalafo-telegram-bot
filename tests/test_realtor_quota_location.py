from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
import random
import pytest
from sqlalchemy import select
from app.inventory import InventoryRepository,plan_period,publication_day_start,as_utc
from app.lalafo.exclusions import is_excluded_contact
from app.models import ApartmentInventoryQueue
from app.location import source_location
from app.telegram.formatting import format_public_apartment
from tests.helpers import make_ad
from tests.test_inventory import _apartments

@pytest.mark.parametrize('phone',['+996 703 108 500','0703-108-500','703108500','+996(703)223711','0703223711','+996703223711; +996555000000'])
def test_blocked_contacts_have_no_format_loophole(phone):
    assert is_excluded_contact(make_ad(phone=phone))
    assert is_excluded_contact(make_ad(source_description='Связь: '+phone))
    assert not is_excluded_contact(make_ad(phone='+996703108501'))

@pytest.mark.parametrize('district,text,expected',[
 ('Центр','МАНАСА/ТОКТОГУЛА. Сдам квартиру','МАНАСА / ТОКТОГУЛА'),
 ('Центр','Филармония\nАдрес: ул. Рыскулова, 15','ул. Рыскулова, 15'),
 ('Центр','Тыныстанова, 30. Мебель и техника','Тыныстанова, 30'),
 ('Ак-Эмир','Ибраимова / Боконбаева','Ибраимова / Боконбаева'),
 (None,'Квартира уютная, есть мебель',None),
])
def test_location_uses_only_source_addresses(district,text,expected):
    assert source_location(district,text)==expected

@pytest.mark.parametrize('kind',['owner','unknown','realtor'])
def test_public_cards_never_display_authorship(kind):
    ad=make_ad(seller_type=kind,owner_listing=kind=='owner',district='Центр',source_description='Адрес: Токтогула / Манаса')
    text=format_public_apartment(ad,bot_username='testbot')
    assert 'Токтогула / Манаса' in text
    assert not any(word in text.casefold() for word in ('собственник','риелтор','риэлтор','автор:','возможно'))

@pytest.mark.parametrize('ordinary_count',[0,1,5,6,10,30,100])
def test_thin_stock_cannot_become_mostly_realtor(ordinary_count):
    normal=_apartments(ordinary_count,central=True,start_id=10000)
    agents=_apartments(100,central=True,start_id=20000,owner=False)
    planned=plan_period(normal+agents,period_start=datetime(2026,11,13,5,tzinfo=timezone(timedelta(hours=6))),rng=random.Random(2))
    assert sum(p.apartment.seller_type=='realtor' for p in planned)<=len(planned)*.15

@pytest.mark.asyncio
async def test_due_queue_rejects_blocked_contact_from_old_inventory(repositories):
    apartments,_,sessions=repositories
    now=publication_day_start(datetime.now(timezone.utc)).astimezone(timezone.utc)+timedelta(hours=2)
    blocked=await apartments.upsert_discovered(make_ad(lalafo_id=7654321,phone='+996 703 108 500'))
    async with sessions.begin() as session:
        session.add(ApartmentInventoryQueue(apartment_id=blocked.id,sequence=1,window_key='old',scheduled_at=now-timedelta(minutes=1)))
    assert await InventoryRepository(sessions).claim_due(now=now) is None
    async with sessions() as session:
        row=await session.scalar(select(ApartmentInventoryQueue).where(ApartmentInventoryQueue.apartment_id==blocked.id))
        assert row.status=='skipped'

@pytest.mark.asyncio
@pytest.mark.parametrize('prior_ordinary,prior_agents,allowed',[(5,0,False),(6,0,True),(12,1,True),(60,10,False)])
async def test_dispatch_hard_limit_uses_actual_published_share(repositories,prior_ordinary,prior_agents,allowed):
    apartments,_,sessions=repositories
    now=publication_day_start(datetime.now(timezone.utc)).astimezone(timezone.utc)+timedelta(hours=19)
    for index in range(prior_ordinary+prior_agents):
        realtor=index>=prior_ordinary
        ad=await apartments.upsert_discovered(make_ad(lalafo_id=8750000+index,seller_type='realtor' if realtor else 'owner',owner_listing=not realtor))
        await apartments.mark_published(ad.id,chat_id=-1001,message_id=8750000+index,acknowledged_at=now-timedelta(minutes=40+index))
    agent=await apartments.upsert_discovered(make_ad(lalafo_id=8760000,seller_type='realtor',owner_listing=False))
    async with sessions.begin() as session:
        session.add(ApartmentInventoryQueue(apartment_id=agent.id,sequence=1,window_key='test',scheduled_at=now-timedelta(minutes=1)))
    row=await InventoryRepository(sessions).claim_due(now=now)
    assert (row is not None)==allowed
