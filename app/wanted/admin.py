from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery

from app.config import Settings
from app.security import TokenSigner
from app.wanted.formatting import format_wanted_ad, format_wanted_public
from app.wanted.keyboards import wanted_payment_keyboard, wanted_public_keyboard
from app.wanted.repository import WantedAdRepository

logger = logging.getLogger(__name__)
router = Router(name="wanted-admin")


def _is_admin(user_id: int, settings: Settings) -> bool:
    return bool(settings.admin_user_id and user_id == settings.admin_user_id)


@router.callback_query(F.data.startswith("wanted-admin:"))
async def wanted_admin_callback(
    callback: CallbackQuery,
    settings: Settings,
    signer: TokenSigner,
    wanted_ads: WantedAdRepository,
    bot: Bot,
) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    parts = (callback.data or "").split(":", 2)
    if len(parts) != 3 or parts[1] not in {"a", "r"}:
        await callback.answer("Недействительная кнопка.", show_alert=True)
        return
    ad_id = signer.verify_id("wanted-admin", parts[2])
    if ad_id is None:
        await callback.answer("Недействительная подпись.", show_alert=True)
        return
    approve = parts[1] == "a"
    outcome = await wanted_ads.begin_decision(
        ad_id, approve=approve, admin_id=callback.from_user.id
    )
    ad = await wanted_ads.get(ad_id)
    if ad is None or outcome == "missing":
        await callback.answer("Заявка не найдена.", show_alert=True)
        return
    if outcome.startswith("already_"):
        await callback.answer("Заявка уже обработана.", show_alert=True)
        return
    if not approve:
        if callback.message:
            await callback.message.edit_text(
                "❌ Оплата заявки отклонена\n\n" + format_wanted_ad(ad)
            )
        try:
            await bot.send_message(
                ad.telegram_user_id,
                "❌ Оплата публикации не подтверждена. Проверьте платеж и повторите попытку.",
                reply_markup=wanted_payment_keyboard(
                    ad.id,
                    signer=signer,
                    payment_url=settings.finik_payment_url,
                    support_url=settings.support_url,
                ),
            )
        except Exception:
            logger.exception("Could not notify rejected wanted ad owner")
        await callback.answer("Оплата отклонена. Клиенту отправлено уведомление.")
        return

    try:
        published = await bot.send_message(
            settings.telegram_group_id,
            format_wanted_public(ad),
            reply_markup=wanted_public_keyboard(ad, support_url=settings.support_url),
        )
    except Exception:
        await wanted_ads.release_publication(ad.id)
        logger.exception("Could not publish wanted ad")
        await callback.answer(
            "Не удалось опубликовать. Заявка возвращена на проверку — нажмите ещё раз.",
            show_alert=True,
        )
        return
    await wanted_ads.mark_published(ad.id, published.message_id)
    if callback.message:
        await callback.message.edit_text(
            "✅ Оплата подтверждена, заявка опубликована\n\n" + format_wanted_ad(ad)
        )
    try:
        await bot.send_message(
            ad.telegram_user_id,
            "✅ Оплата подтверждена. Ваша заявка опубликована в группе.\n\n"
            + format_wanted_ad(ad),
        )
    except Exception:
        logger.exception("Could not notify published wanted ad owner")
    await callback.answer("✅ Заявка опубликована в группе.")
