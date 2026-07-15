"""oikb-gui — простий інтерфейс для синхронізації тек з Open WebUI.

Обгортка над CLI: користувач вводить адресу, API-ключ та ID бази знань,
обирає теку й натискає «Синхронізувати». Коли зібрано через PyInstaller
(див. oikb-gui.spec), той самий exe є і GUI, і власним CLI-бекендом:
`main()` викликає Click CLI, якщо передані аргументи.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from oikb import __version__
from oikb.config import get_config, set_config

POLL_MS = 100


def _cli_cmd(*args: str) -> list[str]:
    """Команда для запуску oikb CLI."""
    if getattr(sys, "frozen", False):
        # Збірка PyInstaller: exe є власним CLI-бекендом.
        return [sys.executable, *args]
    return [sys.executable, "-m", "oikb", *args]


def _popen_kwargs() -> dict:
    """Приховати консольне вікно у Windows."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def _child_env(url: str, token: str) -> dict:
    env = {**os.environ, "NO_COLOR": "1", "PYTHONUTF8": "1"}
    env.pop("PYTHONIOENCODING", None)
    env["OPEN_WEBUI_URL"] = url
    env["OPEN_WEBUI_API_KEY"] = token
    return env


class OikbApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.busy = False

        root.title(f"oikb {__version__} — синхронізація")
        root.geometry("640x520")
        root.minsize(520, 420)

        body = ttk.Frame(root, padding=12)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        # 1. Адреса та API-ключ ------------------------------------
        ttk.Label(body, text="Адреса Open WebUI:").grid(row=0, column=0, sticky="w", pady=4)
        self.url_var = tk.StringVar(value=str(get_config("url") or "https://alph.noone.pw/"))
        ttk.Entry(body, textvariable=self.url_var).grid(row=0, column=1, columnspan=2, sticky="ew", pady=4)

        ttk.Label(body, text="API-ключ:").grid(row=1, column=0, sticky="w", pady=4)
        self.token_var = tk.StringVar(value=str(get_config("token") or ""))
        ttk.Entry(body, textvariable=self.token_var, show="*").grid(
            row=1, column=1, columnspan=2, sticky="ew", pady=4
        )

        # 2. ID бази знань -----------------------------------------
        ttk.Label(body, text="ID бази знань:").grid(row=2, column=0, sticky="w", pady=4)
        self.kb_var = tk.StringVar(value=str(get_config("gui_kb_id") or ""))
        ttk.Entry(body, textvariable=self.kb_var).grid(row=2, column=1, columnspan=2, sticky="ew", pady=4)

        # 3. Тека для синхронізації --------------------------------
        ttk.Label(body, text="Тека:").grid(row=3, column=0, sticky="w", pady=4)
        self.dir_var = tk.StringVar(value=str(get_config("gui_dir") or ""))
        ttk.Entry(body, textvariable=self.dir_var, state="readonly").grid(row=3, column=1, sticky="ew", pady=4)
        ttk.Button(body, text="Обрати теку…", command=self._choose_dir).grid(
            row=3, column=2, sticky="e", padx=(8, 0), pady=4
        )

        # 4. Кнопка синхронізації ----------------------------------
        self.sync_btn = ttk.Button(body, text="Синхронізувати", command=self.on_sync)
        self.sync_btn.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(12, 4))

        # Журнал ---------------------------------------------------
        ttk.Label(body, text="Журнал:").grid(row=5, column=0, sticky="w", pady=(8, 2))
        self.log = scrolledtext.ScrolledText(body, height=12, state="disabled", wrap="word")
        self.log.grid(row=6, column=0, columnspan=3, sticky="nsew")
        body.rowconfigure(6, weight=1)

        self.status_var = tk.StringVar(value="Готово")
        ttk.Label(root, textvariable=self.status_var, anchor="w", padding=(12, 3)).pack(
            fill="x", side="bottom"
        )

        root.after(POLL_MS, self._poll_queue)

    # ── дії ──────────────────────────────────────────────────────

    def _choose_dir(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.dir_var.get() or str(Path.home()))
        if chosen:
            self.dir_var.set(chosen)

    def on_sync(self) -> None:
        if self.busy:
            return
        url = self.url_var.get().strip()
        token = self.token_var.get().strip()
        kb_id = self.kb_var.get().strip()
        directory = self.dir_var.get().strip()

        if not (url and token and kb_id and directory):
            messagebox.showwarning(
                "oikb",
                "Заповніть усі поля: адреса, API-ключ, ID бази знань і тека.",
            )
            return

        # Зберегти для наступного запуску.
        set_config("url", url)
        set_config("token", token)
        set_config("gui_kb_id", kb_id)
        set_config("gui_dir", directory)

        args = ["sync", directory, "--kb-id", kb_id]
        self._set_busy(True)
        self.status_var.set("Синхронізація…")
        self._log(f"\n$ oikb {' '.join(args)}\n")

        cmd = _cli_cmd(*args)
        env = _child_env(url, token)

        def worker() -> None:
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                    **_popen_kwargs(),
                )
            except OSError as exc:
                self.q.put(("line", f"Не вдалося запустити: {exc}\n"))
                self.q.put(("done", 1))
                return
            assert proc.stdout is not None
            for line in proc.stdout:
                self.q.put(("line", line))
            self.q.put(("done", proc.wait()))

        threading.Thread(target=worker, daemon=True).start()

    # ── рушій виводу ─────────────────────────────────────────────

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, *payload = self.q.get_nowait()
                if kind == "line":
                    self._log(payload[0])
                elif kind == "done":
                    code = payload[0]
                    if code == 0:
                        self._log("— Готово ✓\n")
                        self.status_var.set("Готово")
                    else:
                        self._log(f"— Помилка (код {code})\n")
                        self.status_var.set(f"Помилка (код {code})")
                    self._set_busy(False)
        except queue.Empty:
            pass
        self.root.after(POLL_MS, self._poll_queue)

    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.sync_btn.configure(state="disabled" if busy else "normal")


def main() -> None:
    # Прохід до CLI для зібраного exe: `oikb-gui.exe sync ...` працює як CLI.
    if len(sys.argv) > 1:
        from oikb.cli import cli

        cli()
        return
    root = tk.Tk()
    OikbApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
