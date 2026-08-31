from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
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
    no_subletting: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    photo_urls: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    keyboard_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    phone_source_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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


class ApartmentPublicationSchedule(Base):
    """Single shared clock and lease for the three-hour apartment publisher."""

    __tablename__ = "apartment_publication_schedule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="idle"
    )
    lease_token: Mapped[str | None] = mapped_column(String(64), unique=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_published_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


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
    plan: Mapped[str] = mapped_column(String(16), nullable=False, default="week")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    receipt_file_id: Mapped[str | None] = mapped_column(String(255))
    receipt_file_type: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[int | None] = mapped_column(BigInteger)
    access_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_by: Mapped[int | None] = mapped_column(BigInteger)
    admin_message_id: Mapped[int | None] = mapped_column(BigInteger)

    apartment: Mapped[Apartment] = relationship(back_populates="payments")


class SupportTicket(Base):
    __tablename__ = "support_tickets"
    __table_args__ = (
        Index("ix_support_ticket_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(255))
    question: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    admin_message_id: Mapped[int | None] = mapped_column(BigInteger)
    answer: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answered_by: Mapped[int | None] = mapped_column(BigInteger)


class WantedAd(Base):
    __tablename__ = "wanted_ads"
    __table_args__ = (Index("ix_wanted_ads_status_created", "status", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(255))
    rooms: Mapped[str] = mapped_column(String(32), nullable=False)
    district: Mapped[str] = mapped_column(String(255), nullable=False)
    budget: Mapped[int] = mapped_column(Integer, nullable=False)
    move_in: Mapped[str] = mapped_column(String(100), nullable=False)
    tenants: Mapped[str] = mapped_column(String(255), nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False)
    contact: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="awaiting_payment"
    )
    admin_message_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[int | None] = mapped_column(BigInteger)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_by: Mapped[int | None] = mapped_column(BigInteger)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DailyFeaturedPublication(Base):
    __tablename__ = "daily_featured_publications"
    __table_args__ = (
        UniqueConstraint("business_date", "slot", name="uq_featured_business_slot"),
        UniqueConstraint("managed_lalafo_ad_id", name="uq_featured_managed_ad"),
        Index("ix_featured_business_date", "business_date"),
        Index("ix_featured_campaign_status", "campaign_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    slot: Mapped[int] = mapped_column(Integer, nullable=False)
    source_apartment_id: Mapped[int | None] = mapped_column(
        ForeignKey("apartments.id"), nullable=True, index=True
    )
    source_lalafo_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    managed_lalafo_temp_id: Mapped[int | None] = mapped_column(BigInteger)
    managed_lalafo_ad_id: Mapped[int | None] = mapped_column(BigInteger)
    managed_lalafo_ad_url: Mapped[str | None] = mapped_column(Text)
    campaign_id: Mapped[str | None] = mapped_column(String(100))
    campaign_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="not_started"
    )
    campaign_daily_budget: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    managed_lalafo_uploaded_photos: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    lalafo_publication_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="reserved"
    )
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    last_telegram_repeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    new_ad_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expiring_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deactivated_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class FeaturedCandidate(Base):
    __tablename__ = "featured_candidates"
    __table_args__ = (
        UniqueConstraint("business_date", "source_lalafo_id", name="uq_featured_candidate_source"),
        Index("ix_featured_candidate_date_status", "business_date", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    source_apartment_id: Mapped[int | None] = mapped_column(
        ForeignKey("apartments.id"), nullable=True, index=True
    )
    source_lalafo_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="suggested")
    selected_slot: Mapped[int | None] = mapped_column(Integer)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class FeaturedReviewState(Base):
    __tablename__ = "featured_review_state"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    last_update_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class LalafoAutoReplyJob(Base):
    __tablename__ = "lalafo_auto_reply_jobs"
    __table_args__ = (
        UniqueConstraint("ack", name="uq_lalafo_auto_reply_job_ack"),
        Index(
            "ix_lalafo_auto_reply_jobs_ready",
            "status",
            "next_attempt_at",
            "source",
            "inbound_time",
        ),
        Index(
            "ix_lalafo_auto_reply_jobs_chat_order",
            "chat_key",
            "inbound_time",
            "created_at",
            "inbound_key",
        ),
    )

    inbound_key: Mapped[str] = mapped_column(String(600), primary_key=True)
    chat_key: Mapped[str] = mapped_column(String(512), nullable=False)
    inbound_id: Mapped[str] = mapped_column(String(255), nullable=False)
    inbound_time: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ack: Mapped[str] = mapped_column(String(36), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    needs_reconcile: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    first_attempt_at: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class LalafoAutoReplyCursor(Base):
    __tablename__ = "lalafo_auto_reply_cursors"

    chat_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    message_time: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class LalafoAutoReplyMeta(Base):
    __tablename__ = "lalafo_auto_reply_meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
