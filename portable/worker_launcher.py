"""Hidden, self-installing Windows launcher for the dedicated Lalafo link bot."""

from __future__ import annotations

import asyncio
import aiosqlite  # Ensure PyInstaller bundles SQLAlchemy's async SQLite driver.
import ctypes
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
import winreg

from app.bot.lalafo_only import run as run_link_bot
from app.config import get_settings
from scripts.remote_lalafo_collector import run_forever as run_collector


APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ArendaKG" / "LalafoWorker"
ENV_FILE = APP_DIR / ".env"
LOG_FILE = APP_DIR / "worker.log"
INSTALLED_EXE = APP_DIR / "ArendaKG-Lalafo-Worker.exe"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "ArendaKG Lalafo Worker"
RELAY_URL = "https://statutory-mallissa-2danmiller-f1c1b08d.koyeb.app"
TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")
MUTEX_NAME = "Local\\ArendaKG-Lalafo-Worker"


def _quote_env(value: str) -> str:
    return value.replace("\r", "").replace("\n", "").strip()


def _write_configuration(token: str, admin_username: str) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    callback_secret = secrets.token_urlsafe(32)
    ENV_FILE.write_text(
        "\n".join(
            (
                "TELEGRAM_BOT_TOKEN=",
                f"LALAFO_BOT_TOKEN={_quote_env(token)}",
                f"LALAFO_RELAY_URL={RELAY_URL}",
                f"LALAFO_RELAY_SECRET={_quote_env(token)}",
                "TELEGRAM_GROUP_ID=-1004389602150",
                "TELEGRAM_BOT_USERNAME=arenda312bot",
                "ADMIN_USER_ID=0",
                f"ADMIN_USERNAME={_quote_env(admin_username).lstrip('@')}",
                f"CALLBACK_SECRET={callback_secret}",
                "DATABASE_URL=sqlite:///data/bot.db",
                "CITY=Бишкек",
                "MIN_PRICE=20000",
                "MAX_PRICE=45000",
                "ROOMS=1,2",
                "MAX_PHOTOS_PER_APARTMENT=10",
                "ONLY_WITH_PHOTOS=true",
                "ALLOW_NO_DEPOSIT=true",
                "ALLOW_NO_DISTRICT=true",
                "DRY_RUN=false",
                "TEST_MODE=false",
                "LALAFO_PROXY_URL=",
                "LOG_LEVEL=INFO",
                "",
            )
        ),
        encoding="utf-8",
    )


def _register_startup() -> None:
    command = f'"{INSTALLED_EXE}" --worker'
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, command)


def _start_hidden_worker() -> None:
    flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    subprocess.Popen(
        [str(INSTALLED_EXE), "--worker"],
        cwd=APP_DIR,
        creationflags=flags,
        close_fds=True,
    )


def install_gui() -> None:
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.title("Arenda.KG — Lalafo link bot")
    root.geometry("520x250")
    root.resizable(False, False)

    tk.Label(
        root,
        text="Фоновый обработчик ссылок Lalafo",
        font=("Segoe UI", 15, "bold"),
    ).pack(pady=(18, 8))
    tk.Label(root, text="Токен отдельного link-бота:").pack(anchor="w", padx=28)
    token_entry = tk.Entry(root, width=68, show="•")
    token_entry.pack(padx=28, pady=(3, 10))
    tk.Label(root, text="Ваш Telegram username:").pack(anchor="w", padx=28)
    username_entry = tk.Entry(root, width=40)
    username_entry.insert(0, "maxkgz2")
    username_entry.pack(anchor="w", padx=28, pady=(3, 14))

    def install() -> None:
        token = token_entry.get().strip()
        username = username_entry.get().strip().lstrip("@")
        if not TOKEN_RE.fullmatch(token):
            messagebox.showerror("Ошибка", "Введите действующий токен BotFather.")
            return
        if not username:
            messagebox.showerror("Ошибка", "Введите Telegram username администратора.")
            return
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            source_exe = Path(sys.executable).resolve()
            if source_exe != INSTALLED_EXE.resolve():
                shutil.copy2(source_exe, INSTALLED_EXE)
            _write_configuration(token, username)
            (APP_DIR / "data").mkdir(exist_ok=True)
            _register_startup()
            _start_hidden_worker()
        except Exception as exc:
            messagebox.showerror("Ошибка установки", str(exc))
            return
        messagebox.showinfo(
            "Готово",
            "Обработчик запущен без окна и добавлен в автозапуск Windows.",
        )
        root.destroy()

    tk.Button(root, text="Установить и запустить", command=install, width=28).pack()
    root.mainloop()


def worker() -> None:
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not mutex or ctypes.windll.kernel32.GetLastError() == 183:
        return
    APP_DIR.mkdir(parents=True, exist_ok=True)
    os.chdir(APP_DIR)
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    async def run_services() -> None:
        logging.info("Starting Lalafo link bot and combined inventory collector")
        await asyncio.gather(run_link_bot(), run_collector())

    while True:
        try:
            get_settings.cache_clear()
            asyncio.run(run_services())
        except Exception:
            logging.exception("Combined worker stopped; restarting in 10 seconds")
            time.sleep(10)


def main() -> None:
    if "--self-test" in sys.argv:
        return
    if "--worker" in sys.argv:
        worker()
    else:
        install_gui()


if __name__ == "__main__":
    main()
