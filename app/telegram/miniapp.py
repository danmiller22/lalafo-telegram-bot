from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from html import escape
from urllib.parse import parse_qsl


@dataclass(frozen=True, slots=True)
class TelegramMiniAppUser:
    id: int
    first_name: str
    username: str | None = None


def verify_telegram_init_data(
    init_data: str,
    *,
    bot_token: str,
    max_age_seconds: int = 86_400,
    now: int | None = None,
) -> TelegramMiniAppUser | None:
    """Validate Telegram Mini App initData and return its authenticated user."""
    try:
        fields = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
        supplied_hash = fields.pop("hash")
        auth_date = int(fields["auth_date"])
        current_time = int(time.time()) if now is None else now
        if auth_date > current_time + 30 or current_time - auth_date > max_age_seconds:
            return None
        data_check_string = "\n".join(
            f"{key}={value}" for key, value in sorted(fields.items())
        )
        secret_key = hmac.new(
            b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
        ).digest()
        expected_hash = hmac.new(
            secret_key, data_check_string.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(supplied_hash, expected_hash):
            return None
        raw_user = json.loads(fields["user"])
        user_id = int(raw_user["id"])
        first_name = str(raw_user.get("first_name") or "Пользователь")[:255]
        username_value = raw_user.get("username")
        username = str(username_value)[:64] if username_value else None
        return TelegramMiniAppUser(user_id, first_name, username)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def mini_app_html(*, title: str = "Доступ к квартире") -> str:
    safe_title = escape(title)
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
  <title>{safe_title}</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root {{ color-scheme: light dark; font-family: Inter, system-ui, sans-serif; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--tg-theme-bg-color, #f4f6f7); color: var(--tg-theme-text-color, #15201d); }}
    main {{ max-width: 540px; margin: 0 auto; padding: 16px 14px 28px; }}
    .card {{ background: var(--tg-theme-secondary-bg-color, #fff); border-radius: 20px; padding: 16px; box-shadow: 0 8px 28px #00000012; }}
    .hero {{ width: 100%; max-height: 260px; object-fit: cover; border-radius: 15px; display: none; margin-bottom: 14px; }}
    h1 {{ font-size: 21px; margin: 0 0 8px; }}
    .sub {{ color: var(--tg-theme-hint-color, #6c7a76); font-size: 14px; margin-bottom: 14px; }}
    .details {{ line-height: 1.55; white-space: pre-line; margin: 10px 0 16px; }}
    .price {{ font-size: 19px; font-weight: 750; margin: 12px 0 4px; }}
    .status {{ border-radius: 13px; padding: 12px; margin: 12px 0; background: #12856a18; line-height: 1.4; }}
    .phone {{ font-size: 22px; font-weight: 800; color: #079b79; word-break: break-word; }}
    button, .button {{ width: 100%; border: 0; border-radius: 14px; padding: 14px 16px; margin-top: 9px; font: inherit; font-weight: 750; text-align: center; cursor: pointer; text-decoration: none; display: block; }}
    .primary {{ background: var(--tg-theme-button-color, #079b79); color: var(--tg-theme-button-text-color, white); }}
    .secondary {{ background: #12856a18; color: var(--tg-theme-link-color, #07866b); }}
    input[type=file] {{ width: 100%; margin: 10px 0 0; padding: 10px; border: 1px solid #7c8b8738; border-radius: 12px; }}
    .hidden {{ display: none !important; }}
    .foot {{ text-align: center; color: var(--tg-theme-hint-color, #6c7a76); font-size: 12px; margin-top: 14px; }}
  </style>
</head>
<body>
<main>
  <section class="card">
    <img id="hero" class="hero" alt="Квартира">
    <h1 id="title">Загружаем квартиру…</h1>
    <div id="subtitle" class="sub">Без перехода в личный чат и команды /start</div>
    <div id="details" class="details"></div>
    <div id="status" class="status">Проверяем доступ…</div>
    <div id="phone" class="phone hidden"></div>
    <button id="pay" class="primary hidden">Оплатить неделю — 500 сом</button>
    <div id="receipt" class="hidden">
      <input id="file" type="file" accept="image/jpeg,image/png,application/pdf">
      <button id="upload" class="primary">Отправить чек на проверку</button>
    </div>
    <button id="refresh" class="secondary">Обновить статус</button>
  </section>
  <div class="foot">Номер виден только пользователю с подтверждённым доступом</div>
</main>
<script>
(() => {{
  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg) {{ tg.ready(); tg.expand(); }}
  const initData = tg ? tg.initData : "";
  const query = new URLSearchParams(location.search);
  const startParam = (tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param) || query.get("tgWebAppStartParam") || "";
  const el = id => document.getElementById(id);
  let lastState = "";

  function show(id, visible) {{ el(id).classList.toggle("hidden", !visible); }}
  function message(text) {{ el("status").textContent = text; }}
  async function api(path, body) {{
    const response = await fetch(path, {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{init_data: initData, start_param: startParam, ...body}})
    }});
    const data = await response.json().catch(() => ({{detail: "Ошибка сервиса"}}));
    if (!response.ok) throw new Error(data.detail || "Ошибка сервиса");
    return data;
  }}
  function render(data) {{
    lastState = data.status;
    el("title").textContent = data.title || "Квартира";
    el("details").textContent = data.details || "";
    if (data.photo_url) {{ el("hero").src = data.photo_url; el("hero").style.display = "block"; }}
    show("phone", data.status === "approved");
    show("pay", ["unpaid", "rejected"].includes(data.status));
    show("receipt", data.status === "awaiting_receipt");
    if (data.status === "approved") {{
      el("phone").textContent = "📞 " + data.phone;
      message("✅ Доступ активен" + (data.expires_at_text ? " до " + data.expires_at_text : ""));
    }} else if (data.status === "pending") {{
      message("⏳ Чек проверяется. Квартира сохранена — закройте окно и вернитесь позже.");
    }} else if (data.status === "awaiting_receipt") {{
      message("После оплаты прикрепите фото или PDF чека ниже.");
    }} else if (data.status === "rejected") {{
      message("Оплата не подтверждена. Можно повторить оплату и отправить новый чек.");
    }} else {{
      message("Неделя доступа ко всем номерам — 500 сом.");
    }}
  }}
  async function load() {{
    if (!initData || !startParam) {{
      message("Откройте это окно кнопкой под карточкой квартиры в Telegram.");
      show("refresh", false);
      return;
    }}
    try {{ render(await api("/miniapp/api/session", {{}})); }}
    catch (error) {{ message(error.message); }}
  }}
  el("pay").onclick = async () => {{
    el("pay").disabled = true;
    try {{
      const data = await api("/miniapp/api/start", {{}});
      render(data);
      if (tg) tg.openLink(data.payment_url); else location.href = data.payment_url;
    }} catch (error) {{ message(error.message); }}
    finally {{ el("pay").disabled = false; }}
  }};
  el("upload").onclick = async () => {{
    const file = el("file").files[0];
    if (!file) {{ message("Сначала выберите фото или PDF чека."); return; }}
    if (file.size > 10 * 1024 * 1024) {{ message("Файл должен быть не больше 10 МБ."); return; }}
    el("upload").disabled = true;
    message("Отправляем чек…");
    try {{
      const encoded = await new Promise((resolve, reject) => {{
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result).split(",", 2)[1]);
        reader.onerror = reject;
        reader.readAsDataURL(file);
      }});
      render(await api("/miniapp/api/receipt", {{file_name: file.name, content_type: file.type, file_base64: encoded}}));
    }} catch (error) {{ message(error.message); }}
    finally {{ el("upload").disabled = false; }}
  }};
  el("refresh").onclick = load;
  load();
  setInterval(() => {{ if (lastState === "pending") load(); }}, 5000);
}})();
</script>
</body>
</html>"""
