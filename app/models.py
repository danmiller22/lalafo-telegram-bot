from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Apartment(Base):
    __tablename__ = "apartments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lalafo_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    phone: Mapped[str] = mapped_column(String(32), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    rooms: Mapped[str] = mapped_column(String(16), nullable=False)
    district: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    deposit: Mapped[int | None] = mapped_column(Integer)
    photo_urls: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    publication_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="discovered"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    payments: Mapped[list["PaymentRequest"]] = relationship(back_populates="apartment")


class PaymentRequest(Base):
    __tablename__ = "payment_requests"
    __table_args__ = (
        UniqueConstraint("telegram_user_id", "apartment_id", name="uq_payment_access"),
        Index("ix_payment_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    apartment_id: Mapped[int] = mapped_column(
        ForeignKey("apartments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[int | None] = mapped_column(BigInteger)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_by: Mapped[int | None] = mapped_column(BigInteger)
    admin_message_id: Mapped[int | None] = mapped_column(BigInteger)

    apartment: Mapped[Apartment] = relationship(back_populates="payments")
