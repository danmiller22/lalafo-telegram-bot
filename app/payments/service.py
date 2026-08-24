from __future__ import annotations

from dataclasses import dataclass

from app.models import Apartment, PaymentRequest
from app.payments.repository import ApartmentRepository, PaymentRepository, PaymentSubmission


@dataclass(frozen=True)
class ContactResult:
    status: str
    apartment: Apartment | None


class PaymentService:
    def __init__(
        self, apartments: ApartmentRepository, payments: PaymentRepository, *, admin_user_id: int
    ) -> None:
        self.apartments = apartments
        self.payments = payments
        self.admin_user_id = admin_user_id

    async def contact_status(self, user_id: int, apartment_id: int) -> ContactResult:
        request = await self.payments.get_access(user_id, apartment_id)
        if request is not None:
            apartment = request.apartment
            if apartment is None or not apartment.active or not apartment.phone:
                return ContactResult("unavailable", apartment)
            return ContactResult(request.status, apartment)
        apartment = await self.apartments.get(apartment_id)
        if apartment is None or not apartment.active or not apartment.phone:
            return ContactResult("unavailable", apartment)
        return ContactResult("unpaid", apartment)

    async def submit_payment(
        self,
        *,
        user_id: int,
        apartment_id: int,
        username: str | None,
        first_name: str | None,
    ) -> PaymentSubmission:
        return await self.payments.submit(
            user_id=user_id,
            apartment_id=apartment_id,
            username=username,
            first_name=first_name,
        )

    async def decide(self, request_id: int, *, approve: bool, actor_id: int) -> str:
        if not self.admin_user_id or actor_id != self.admin_user_id:
            return "forbidden"
        return await self.payments.decide(request_id, approve=approve, admin_id=actor_id)

    async def get_request(self, request_id: int) -> PaymentRequest | None:
        return await self.payments.get_request(request_id)
