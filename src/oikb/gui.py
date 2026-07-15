"""oikb-gui — простий інтерфейс для синхронізації тек з Open WebUI.

Користувач вводить адресу та API-ключ, обирає базу знань зі списку (назви
підтягуються через API), обирає теку й натискає «Синхронізувати». Після
завершення показується докладна статистика: скільки файлів знайдено,
пропущено, вже було в базі, завантажено та скільки не вдалося обробити.

Синхронізація виконується у процесі (через API oikb) у окремому потоці,
щоб інтерфейс не «підвисав». Коли додаток зібрано через PyInstaller,
той самий exe є і GUI, і CLI-бекендом (див. main()).
"""

from __future__ import annotations

import queue
import sys
import threading
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from oikb import __version__
from oikb.client import OikbClient
from oikb.config import get_config, set_config

DEFAULT_URL = "https://alph.noone.pw/"
POLL_MS = 100


def _kb_file_count(client: OikbClient, kb_id: str) -> int | None:
    """Скільки файлів зараз у базі знань (None, якщо не вдалося дізнатись)."""
    try:
        return client.get_kb_file_count(kb_id)
    except Exception:
        return None


class OikbApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.kb_map: dict[str, str] = {}  # назва → id
        self.allowed_exts: list[str] = []  # дозволені сервером (порожньо = усі)

        root.title(f"oikb {__version__} — синхронізація")
        root.geometry("680x620")
        root.minsize(560, 520)

        body = ttk.Frame(root, padding=12)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        # 1. Адреса та API-ключ ------------------------------------
        ttk.Label(body, text="Адреса Open WebUI:").grid(row=0, column=0, sticky="w", pady=4)
        self.url_var = tk.StringVar(value=str(get_config("url") or DEFAULT_URL))
        ttk.Entry(body, textvariable=self.url_var).grid(row=0, column=1, columnspan=2, sticky="ew", pady=4)

        ttk.Label(body, text="API-ключ:").grid(row=1, column=0, sticky="w", pady=4)
        self.token_var = tk.StringVar(value=str(get_config("token") or ""))
        ttk.Entry(body, textvariable=self.token_var, show="*").grid(
            row=1, column=1, sticky="ew", pady=4
        )
        ttk.Button(body, text="Зберегти й оновити", command=self.on_save).grid(
            row=1, column=2, sticky="e", padx=(8, 0), pady=4
        )

        # 2. База знань (за назвою) --------------------------------
        ttk.Label(body, text="База знань:").grid(row=2, column=0, sticky="w", pady=4)
        self.kb_var = tk.StringVar()
        self.kb_combo = ttk.Combobox(body, textvariable=self.kb_var, values=[], state="normal")
        self.kb_combo.grid(row=2, column=1, sticky="ew", pady=4)
        ttk.Button(body, text="Оновити список", command=self._load_kbs).grid(
            row=2, column=2, sticky="e", padx=(8, 0), pady=4
        )
        self.kb_status = tk.StringVar(value="")
        ttk.Label(body, textvariable=self.kb_status, foreground="gray").grid(
            row=3, column=1, columnspan=2, sticky="w"
        )

        # 3. Тека для синхронізації --------------------------------
        ttk.Label(body, text="Тека:").grid(row=4, column=0, sticky="w", pady=4)
        self.dir_var = tk.StringVar(value=str(get_config("gui_dir") or ""))
        ttk.Entry(body, textvariable=self.dir_var, state="readonly").grid(row=4, column=1, sticky="ew", pady=4)
        ttk.Button(body, text="Обрати теку…", command=self._choose_dir).grid(
            row=4, column=2, sticky="e", padx=(8, 0), pady=4
        )

        # 4. Кнопка синхронізації + прогрес ------------------------
        self.sync_btn = ttk.Button(body, text="Синхронізувати", command=self.on_sync)
        self.sync_btn.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(12, 4))

        self.progress = ttk.Progressbar(body, mode="determinate")
        self.progress.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(0, 4))

        # Журнал / статистика --------------------------------------
        ttk.Label(body, text="Результат:").grid(row=9, column=0, sticky="w", pady=(8, 2))
        self.log = scrolledtext.ScrolledText(body, height=14, state="disabled", wrap="word")
        self.log.grid(row=10, column=0, columnspan=3, sticky="nsew")
        body.rowconfigure(10, weight=1)

        self.status_var = tk.StringVar(value="Готово")
        ttk.Label(root, textvariable=self.status_var, anchor="w", padding=(12, 3)).pack(
            fill="x", side="bottom"
        )

        # Стартове завантаження списку баз, якщо є адреса й ключ.
        self._saved_kb_id = str(get_config("gui_kb_id") or "")
        if self.url_var.get().strip() and self.token_var.get().strip():
            self._load_kbs()

        root.after(POLL_MS, self._poll_queue)

    # ── база знань ───────────────────────────────────────────────

    def on_save(self) -> None:
        url = self.url_var.get().strip()
        token = self.token_var.get().strip()
        if url:
            set_config("url", url)
        if token:
            set_config("token", token)
        self._load_kbs()

    def _load_kbs(self) -> None:
        url = self.url_var.get().strip()
        token = self.token_var.get().strip()
        if not (url and token):
            self.kb_status.set("Введіть адресу та API-ключ, щоб завантажити список.")
            return
        self.kb_status.set("Завантаження списку баз…")

        def worker() -> None:
            try:
                with OikbClient(url, token) as client:
                    kbs = client.list_knowledge_bases()
                    try:
                        allowed = client.get_allowed_file_extensions()
                    except Exception:
                        allowed = []  # немає доступу до конфігу — не обмежуємо
                items = [
                    (str(kb.get("name") or "(без назви)"), str(kb.get("id")))
                    for kb in kbs
                    if kb.get("id")
                ]
                self.q.put(("kb_list", items))
                self.q.put(("allowed_exts", allowed))
            except Exception as exc:
                self.q.put(("kb_error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_kb_list(self, items: list[tuple[str, str]]) -> None:
        self.kb_map = {name: kb_id for name, kb_id in items}
        self.kb_combo["values"] = [name for name, _ in items]
        if not items:
            self.kb_status.set("Баз знань не знайдено для цього ключа.")
            return
        self.kb_status.set(f"Знайдено баз: {len(items)}")
        # Відновити раніше обрану базу за збереженим id.
        if self._saved_kb_id:
            for name, kb_id in items:
                if kb_id == self._saved_kb_id:
                    self.kb_var.set(name)
                    return
        if not self.kb_var.get():
            self.kb_var.set(items[0][0])

    def _resolve_kb_id(self) -> str:
        """Назва зі списку → id; якщо введено вручну — використати як id."""
        text = self.kb_var.get().strip()
        return self.kb_map.get(text, text)

    def _include_globs(self) -> list[str] | None:
        """Glob-шаблони з дозволених сервером розширень (None = без обмежень)."""
        if not self.allowed_exts:
            return None
        return [f"*.{e}" for e in self.allowed_exts]

    def _apply_allowed_exts(self, exts: list[str]) -> None:
        # Дотримуємось налаштувань сервера мовчки: якщо він обмежує типи,
        # інші файли буде пропущено (це видно у статистиці «Пропущено фільтром»).
        self.allowed_exts = exts

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
        kb_id = self._resolve_kb_id()
        directory = self.dir_var.get().strip()

        if not (url and token and kb_id and directory):
            messagebox.showwarning(
                "oikb",
                "Заповніть усі поля: адреса, API-ключ, база знань і тека.",
            )
            return
        if not Path(directory).is_dir():
            messagebox.showerror("oikb", f"Теку не знайдено:\n{directory}")
            return

        include = self._include_globs()

        # Зберегти для наступного запуску.
        set_config("url", url)
        set_config("token", token)
        set_config("gui_kb_id", kb_id)
        set_config("gui_dir", directory)

        self._set_busy(True)
        self._clear_log()
        self.progress.configure(mode="determinate", value=0, maximum=100)
        self.status_var.set("Підготовка…")
        self._log(f"Синхронізація теки:\n  {directory}\n→ база знань {kb_id}\n")
        if include:
            self._log(f"Лише дозволені сервером розширення: {', '.join(self.allowed_exts)}\n")
        self._log("\n")

        from oikb.sync import run_sync, build_manifest_filter
        from oikb.connectors.filesystem import FilesystemConnector

        def worker() -> None:
            client = OikbClient(url, token)
            try:
                before = _kb_file_count(client, kb_id)

                def cb(done: int, total: int) -> None:
                    self.q.put(("progress", done, total))

                connector = FilesystemConnector(directory)
                result = run_sync(
                    client=client,
                    connector=connector,
                    kb_id=kb_id,
                    quiet=True,
                    progress_callback=cb,
                    manifest_filter=build_manifest_filter(include=include),
                )
                after = _kb_file_count(client, kb_id)
                self.q.put(("sync_done", result, before, after))
            except Exception as exc:
                self.q.put(("sync_error", str(exc)))
            finally:
                client.close()

        threading.Thread(target=worker, daemon=True).start()

    # ── рушій виводу ─────────────────────────────────────────────

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, *payload = self.q.get_nowait()
                if kind == "kb_list":
                    self._apply_kb_list(payload[0])
                elif kind == "allowed_exts":
                    self._apply_allowed_exts(payload[0])
                elif kind == "kb_error":
                    self.kb_status.set(f"Не вдалося завантажити список: {payload[0]}")
                elif kind == "progress":
                    done, total = payload
                    self.progress.configure(maximum=max(total, 1), value=done)
                    self.status_var.set(f"Завантаження {done}/{total}…")
                elif kind == "sync_done":
                    self._render_stats(*payload)
                    self._set_busy(False)
                elif kind == "sync_error":
                    self._log(f"\n✗ Помилка синхронізації: {payload[0]}\n")
                    self.status_var.set("Помилка")
                    self._set_busy(False)
        except queue.Empty:
            pass
        self.root.after(POLL_MS, self._poll_queue)

    def _render_stats(self, result, before: int | None, after: int | None) -> None:
        errors = result.errors or []
        self.progress.configure(value=self.progress["maximum"])

        rows = [
            ("Знайдено у теці", result.found),
            ("Пропущено під час сканування", result.scan_skipped),
            ("Пропущено фільтром/розміром", result.skipped_filter),
            ("Уже в базі (без змін)", result.unmodified),
            ("Додано", result.added),
            ("Оновлено", result.modified),
            ("Видалено", result.deleted),
            ("Помилок завантаження", len(errors)),
        ]
        self._log("── Статистика ──────────────────────────\n")
        for label, value in rows:
            self._log(f"  {label:<32}{value:>6}\n")

        # Розбивка за розширенням файлів.
        by_ext = result.by_ext or {}
        if by_ext:
            self._log("\n── За розширенням ──────────────────────\n")
            self._log(f"  {'Тип':<18}{'Знайдено':>9}{'Завант.':>9}{'Помилок':>9}\n")
            for ext, c in sorted(by_ext.items(), key=lambda kv: -kv[1]["found"]):
                self._log(
                    f"  {ext:<18}{c['found']:>9}{c['uploaded']:>9}{c['failed']:>9}\n"
                )

        if before is not None and after is not None:
            self._log("\n")
            self._log(f"  {'Файлів у базі було':<32}{before:>6}\n")
            self._log(f"  {'Файлів у базі стало':<32}{after:>6}\n")

            # Скільки мало з'явитись проти того, що реально з'явилось.
            net_expected = result.added - result.deleted
            net_actual = after - before
            gap = net_expected - net_actual
            if gap > 0:
                self._log(
                    f"\n⚠ Кількість файлів у базі зросла на {net_actual}, "
                    f"хоча завантажено {net_expected}.\n"
                    f"  {gap} файл(ів) могли бути дублікатами (той самий вміст уже\n"
                    "  є в базі) або сервер не зміг обробити їхній вміст.\n"
                )

        # Пояснення розриву «знайдено» ↔ «завантажено».
        not_uploaded = result.found - result.added - result.modified
        if result.unmodified and not_uploaded > 0:
            self._log(
                f"\nℹ Із {result.found} знайдених файлів {result.unmodified} уже були\n"
                "  в базі й не завантажувались повторно (синхронізація інкрементна).\n"
            )

        if errors:
            self._log(f"\n── Не вдалося завантажити ({len(errors)}) ──\n")
            for err in errors[:200]:
                self._log(f"  • {err}\n")
            if len(errors) > 200:
                self._log(f"  … та ще {len(errors) - 200}\n")

        if errors or (before is not None and after is not None and after - before < result.added - result.deleted):
            self.status_var.set("Завершено з попередженнями")
        else:
            self._log("\n✓ Готово\n")
            self.status_var.set("Готово")

    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
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
