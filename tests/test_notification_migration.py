"""The notification lease migration preserves approvals and payment history."""
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa

from app.models import Base, Apartment, PaymentRequest, PaymentHistory


def test_notification_migration_preserves_existing_requests_and_ledger(monkeypatch):
    path = Path(__file__).parents[1] / "alembic/versions/0030_payment_notification_lease.py"
    spec = importlib.util.spec_from_file_location("notification_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine("sqlite:///:memory:")
    now = datetime.now(timezone.utc)
    try:
        with engine.begin() as connection:
            Base.metadata.create_all(connection)
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
            migration.downgrade()
            connection.execute(Apartment.__table__.insert().values(id=1, lalafo_id=1,
                source_url="https://lalafo.kg/bishkek/ads/id-1", phone="+996555111111",
                fingerprint="test", price=30000, rooms="1", city="Бишкек"))
            connection.execute(PaymentRequest.__table__.insert().values(id=1,
                telegram_user_id=1, apartment_id=1, status="approved", plan="week",
                approved_at=now, approved_by=999, access_expires_at=now + timedelta(days=7),
                provider_payment_id="paid"))
            connection.execute(PaymentRequest.__table__.insert().values(id=2,
                telegram_user_id=2, apartment_id=1, status="pending", plan="week", admin_message_id=-1))
            connection.execute(PaymentHistory.__table__.insert().values(payment_request_id=1,
                telegram_user_id=1, apartment_id=1, plan="week", amount=499,
                provider_payment_id="paid", paid_at=now, access_expires_at=now + timedelta(days=7)))
            before = connection.execute(sa.text("SELECT * FROM payment_requests ORDER BY id")).mappings().all()
            migration.upgrade()
            after = connection.execute(sa.text("SELECT * FROM payment_requests ORDER BY id")).mappings().all()
            for old, new in zip(before, after):
                assert dict(old) == {key: new[key] for key in old}
                assert new["admin_notification_token"] is None
                assert new["admin_notification_claimed_at"] is None
            assert connection.scalar(sa.text("SELECT COUNT(*) FROM payment_history")) == 1
            assert connection.scalar(sa.text("SELECT amount FROM payment_history")) == 499
            migration.downgrade()
            assert connection.scalar(sa.text("SELECT COUNT(*) FROM payment_requests")) == 2
            migration.upgrade()
    finally:
        engine.dispose()
