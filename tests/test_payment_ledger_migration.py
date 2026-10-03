from datetime import datetime
import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa

from app.models import PaymentHistory


@pytest.mark.parametrize("existing_ledger", [False, True])
def test_lifetime_ledger_migration_supports_fresh_and_existing_database(monkeypatch, existing_ledger):
    path = Path(__file__).parents[1] / "alembic/versions/0026_lifetime_payment_access.py"
    spec = importlib.util.spec_from_file_location("lifetime_ledger_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        if existing_ledger:
            table = PaymentHistory.__table__.to_metadata(sa.MetaData())
            table.c.access_expires_at.nullable = False
            table.create(connection)
            connection.execute(table.insert().values(payment_request_id=1, telegram_user_id=77,
                apartment_id=42, plan="week", amount=499, provider_payment_id="old-payment",
                paid_at=datetime(2026, 1, 1), access_expires_at=datetime(2026, 1, 8)))
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        columns = {c["name"]: c for c in sa.inspect(connection).get_columns("payment_history")}
        assert columns["access_expires_at"]["nullable"]
        assert {i["name"] for i in sa.inspect(connection).get_indexes("payment_history")} == {
            "ix_payment_history_user_paid", "ix_payment_history_provider_id"}
        assert connection.scalar(sa.text("SELECT COUNT(*) FROM payment_history")) == int(existing_ledger)
        connection.execute(PaymentHistory.__table__.insert().values(payment_request_id=2,
            telegram_user_id=88, apartment_id=43, plan="lifetime", amount=699,
            provider_payment_id="lifetime-payment", paid_at=datetime(2026, 1, 2), access_expires_at=None))
        migration.downgrade()
        assert connection.scalar(sa.text("SELECT COUNT(*) FROM payment_history")) == 1 + int(existing_ledger)
        assert connection.scalar(sa.text("SELECT COUNT(*) FROM payment_history WHERE access_expires_at IS NULL")) == 0
        migration.upgrade()
    engine.dispose()
