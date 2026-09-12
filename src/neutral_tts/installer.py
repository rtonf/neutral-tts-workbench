"""User-level graphical installer using the owned source-layout transactions."""
from __future__ import annotations

import os
import queue
from pathlib import Path

from .desktop import WorkerProcess
from .identity import DISPLAY_NAME


def main():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    window = tk.Tk()
    window.title(DISPLAY_NAME + " Setup")
    window.geometry("800x410")
    frame = ttk.Frame(window, padding=20)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text=DISPLAY_NAME + " Setup", font=("Segoe UI", 17, "bold")).pack(anchor="w")
    ttk.Label(frame, text="未署名のローカル候補。Python 3.11以降とTkが別途必要です。\n新規Windows・実TTSの受け入れ試験は未完了です。", wraplength=750).pack(anchor="w", pady=12)
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    values = {"layout_root": tk.StringVar(value=str(Path(__file__).absolute().parents[2])),
              "app_root": tk.StringVar(value=str(local / "Programs" / "neutral-tts-workbench")),
              "data_root": tk.StringVar(value=str(local / "neutral-tts-workbench"))}
    labels = {"layout_root": "配布候補フォルダー", "app_root": "アプリ配置先", "data_root": "利用者データ"}
    controls = []
    for key, value in values.items():
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=6)
        ttk.Label(row, text=labels[key], width=20).pack(side="left")
        entry = ttk.Entry(row, textvariable=value)
        entry.pack(side="left", fill="x", expand=True)
        controls.append(entry)
        def select(key=key):
            selected = filedialog.askdirectory(parent=window, mustexist=False)
            if selected:
                values[key].set(selected)
        button = ttk.Button(row, text="選択", command=select)
        button.pack(side="left", padx=5)
        controls.append(button)
    state = {"worker": None}
    status = tk.StringVar(value="選択したフォルダーを確認してください。WAV・利用者データを自動削除しません。")
    def run(operation):
        if state["worker"] and state["worker"].busy:
            return
        app = values["app_root"].get()
        data = values["data_root"].get()
        if not messagebox.askokcancel("対象の確認", operation + "\nアプリ：" + app + "\nデータ：" + data, parent=window):
            return
        # Creation is restricted to the user's explicitly selected parent.
        from .cli import _safe_directory
        try:
            _safe_directory(str(Path(app).absolute().parent), label="install parent")
            worker = WorkerProcess(Path(data).absolute())
            state["worker"] = worker
            worker.start("lifecycle", {"operation": operation, "app_root": app, "layout_root": values["layout_root"].get()})
            for control in controls:
                control.configure(state="disabled")
            status.set("処理中…終了後に launch_desktop.pyw から起動してください。")
        except Exception:
            status.set("配置先を作成できません。権限とフォルダーを確認してください。")
    row = ttk.Frame(frame)
    row.pack(fill="x", pady=15)
    for operation, title in (("install", "インストール"), ("update", "更新"), ("rollback", "ロールバック"), ("uninstall", "アンインストール")):
        button = ttk.Button(row, text=title, command=lambda operation=operation: run(operation))
        button.pack(side="left", padx=4)
        controls.append(button)
    ttk.Label(frame, textvariable=status, wraplength=750).pack(anchor="w")
    def poll():
        worker = state["worker"]
        if worker:
            try:
                _, result = worker.events.get_nowait()
            except queue.Empty:
                pass
            else:
                status.set("完了しました。利用者データは保持されています。" if result["status"] == "ok" else "完了できません。対象名・候補のOWNED_FILES.json・変更されたファイル・容量を確認してください。")
                for control in controls:
                    control.configure(state="normal")
        window.after(100, poll)
    def close():
        if state["worker"] and state["worker"].busy:
            status.set("処理の完了後に閉じてください。")
        else:
            window.destroy()
    window.protocol("WM_DELETE_WINDOW", close)
    window.after(100, poll)
    window.mainloop()
    return 0
