"""Tk desktop with cancellable process-isolated operations and local path UI."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

from .identity import DISPLAY_NAME
from .filename_templates import DEFAULT_TEMPLATE


class WorkerProcess:
    def __init__(self, data_root, *, python=None):
        self.data_root = str(data_root)
        self.python = python or sys.executable
        self.events = queue.Queue()
        self.process = None
        self.owner = None
        self.lock = threading.Lock()
        self.busy = False
        self.cancelled = False

    def start(self, operation, arguments):
        with self.lock:
            if self.busy:
                raise RuntimeError("A worker is already active.")
            self.busy, self.cancelled = True, False
        thread = threading.Thread(target=self._run, args=(operation, arguments), daemon=True)
        thread.start()

    def _run(self, operation, arguments):
        try:
            # Explicit trusted source location; input text and user paths travel
            # only through the pipe, never command arguments or a shell.
            source = str(Path(__file__).absolute().parents[1])
            bootstrap = "import sys;sys.path.insert(0,sys.argv[1]);from neutral_tts.desktop_worker import main;raise SystemExit(main())"
            environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
            with self.lock:
                if self.cancelled:
                    raise RuntimeError("Cancelled before process creation.")
                self.process = subprocess.Popen(
                    [sys.executable if operation in {"setup_environment", "setup_inference"} else self.python, "-I", "-B", "-c", bootstrap, source],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    env=environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                process = self.process
                from .process_owner import ProcessOwner
                self.owner = ProcessOwner(process)
            request = {"operation": operation, "data_root": self.data_root, "arguments": arguments}
            output, _ = process.communicate(json.dumps(request, ensure_ascii=True).encode())
            if len(output) > 8 * 1024**2:
                raise RuntimeError("Worker response exceeds its bound.")
            result = json.loads(output)
        except Exception:
            result = {"status": "error", "code": "WORKER_FAILED"}
        with self.lock:
            cancelled = self.cancelled
            if self.owner:
                self.owner.close()
                self.owner = None
            self.process = None
            self.busy = False
        self.events.put((operation, {"status": "cancelled"} if cancelled else result))

    def cancel(self):
        with self.lock:
            self.cancelled = True
            if self.owner is not None:
                self.owner.cancel()


class Desktop:
    def __init__(self, window, data_root, *, python=None):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk, self.window = tk, ttk, window
        if python is None:
            selection = Path(data_root) / "local-interpreter.json"
            if selection.exists():
                try:
                    from .workbench import read_private
                    from .local_runtime import _verify_asset
                    binding = read_private(selection)
                    executable = Path(binding["python"])
                    _verify_asset(executable, {"size": executable.stat().st_size, "sha256": binding["sha256"]})
                    python = str(executable)
                except Exception:
                    python = None
        self.worker = WorkerProcess(data_root, python=python)
        self.controls = []
        self.current_operation = None
        self.history_ids = []
        self.history_paths = []
        self.preview_ready = False
        self.settings_dirty = False
        self.window.title(DISPLAY_NAME)
        self.window.geometry("980x760")
        self.window.minsize(800, 640)
        outer = ttk.Frame(window, padding=14)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text=DISPLAY_NAME, font=("Segoe UI", 18, "bold")).pack(anchor="w")
        ttk.Label(outer, text="未署名のWindows配布候補  |  音声は選択した保存先へ保存します").pack(anchor="w", pady=(4, 8))
        self.tabs = ttk.Notebook(outer)
        self.tabs.pack(fill="both", expand=True)
        generate = ttk.Frame(self.tabs, padding=10)
        setup = ttk.Frame(self.tabs, padding=10)
        history = ttk.Frame(self.tabs, padding=10)
        maintenance = ttk.Frame(self.tabs, padding=10)
        for frame, title in ((generate, "音声を作る"), (setup, "初回設定・辞書"), (history, "履歴・再生"), (maintenance, "保全・更新")):
            self.tabs.add(frame, text=title)
        from .user_guidance import show_guidance
        guidance = ttk.Frame(generate)
        guidance.pack(fill="x", pady=(0, 6))
        ttk.Label(guidance, text="録音の権利・声の複製同意・用途を確認し、公開時は合成音声と分かる表示に。", wraplength=500).pack(side="left", fill="x", expand=True)
        ttk.Button(guidance, text="権利・責任・相談先", command=lambda: show_guidance(window)).pack(side="right", padx=(8, 0))
        ttk.Label(generate, text="読み上げる文章（4096文字まで）").pack(anchor="w")
        self.text = tk.Text(generate, height=12, wrap="word", undo=True)
        self.text.pack(fill="both", expand=True, pady=8)
        self.text.bind("<<Modified>>", self._input_changed)
        row = ttk.Frame(generate)
        row.pack(fill="x")
        self.demo = tk.BooleanVar(value=False)
        toggle = ttk.Checkbutton(row, text="合成テスト（無音）", variable=self.demo, command=self._invalidate)
        toggle.pack(side="left")
        self.controls.append(toggle)
        self._button(row, "UTF-8ファイルを開く", self._open_text).pack(side="right")
        row = ttk.Frame(generate)
        row.pack(fill="x", pady=8)
        self._button(row, "プレビュー", self._preview).pack(side="left")
        self.generate_button = self._button(row, "この内容で生成", self._generate)
        self.generate_button.pack(side="left", padx=8)
        self.generate_button.configure(state="disabled")
        self.destination = tk.StringVar(value="保存先はプレビュー後にここへ表示します。")
        ttk.Label(generate, textvariable=self.destination, wraplength=850).pack(anchor="w")
        self.preview_text = tk.StringVar(value="実プレビューには初回設定で確認済みのトークナイザーを指定してください。")
        ttk.Label(generate, textvariable=self.preview_text, wraplength=850).pack(anchor="w", pady=8)
        self.settings = {}
        row = ttk.Frame(setup)
        row.pack(fill="x", pady=4)
        self._button(row, "導入前の環境確認", lambda: self._start("environment", {})).pack(side="left")
        self._button(row, "確認済みトークナイザーを取得（約8.6 MB）", self._setup_tokenizer).pack(side="left", padx=8)
        self._button(setup, "専用Python環境を作成・修復（Windows/Python 3.13）", self._setup_environment).pack(anchor="w", pady=3)
        row = ttk.Frame(setup)
        row.pack(fill="x", pady=3)
        self._button(row, "参照音声と利用範囲を登録", self._reference_setup).pack(side="left")
        self._button(row, "登録済み参照を検査・表示", lambda: self._start("current_references", {})).pack(side="left", padx=8)
        self._button(setup, "実生成の初回セットアップ（モデル・CUDA・AudioSeal）", self._setup_inference).pack(anchor="w", pady=3)
        defaults = {"output_directory": str(Path(data_root).parent / "neutral-tts-output"),
                    "tokenizer_config": "", "runtime_config": "", "title": "audio",
                    "template": DEFAULT_TEMPLATE, "pause_seconds": "0"}
        labels = {"output_directory": "WAV保存先", "tokenizer_config": "トークナイザー設定", "runtime_config": "実生成の権利・資産設定",
                  "title": "保存名のタイトル", "template": "保存名テンプレート", "pause_seconds": "チャンク間の無音（秒）"}
        for key, default in defaults.items():
            row = ttk.Frame(setup)
            row.pack(fill="x", pady=3)
            ttk.Label(row, text=labels[key], width=25).pack(side="left")
            variable = tk.StringVar(value=default)
            self.settings[key] = variable
            variable.trace_add("write", self._setting_changed)
            entry = ttk.Entry(row, textvariable=variable)
            entry.pack(side="left", fill="x", expand=True)
            self.controls.append(entry)
            if key in {"output_directory", "tokenizer_config", "runtime_config"}:
                self._button(row, "選択", lambda key=key: self._select_setting(key)).pack(side="left", padx=4)
        ttk.Label(setup, text="辞書：1行につき「表記<TAB>読み」。設定と辞書はこの端末に保存します。").pack(anchor="w", pady=(10, 4))
        self.dictionary = tk.Text(setup, height=5, wrap="none", undo=True)
        self.dictionary.pack(fill="both", expand=True)
        self.dictionary.bind("<<Modified>>", self._input_changed)
        self._button(setup, "設定・辞書を保存", self._save).pack(anchor="e", pady=8)
        ttk.Label(setup, text="未確認の重みは取得・実行できません。参照録音の利用権、声の複製同意、生成音声の公開範囲を別々に確認してください。", wraplength=850).pack(anchor="w")
        self.history = tk.Listbox(history, height=16, exportselection=False)
        self.history.pack(fill="both", expand=True)
        self.history_destination = tk.StringVar(value="選択したWAVの場所をここへ表示します。")
        self.history.bind("<<ListboxSelect>>", self._history_selected)
        ttk.Label(history, textvariable=self.history_destination, wraplength=850).pack(anchor="w", pady=6)
        self._button(history, "選択したWAVを検査して再生", self._play).pack(anchor="e", pady=8)
        self._button(history, "保存WAVのAudioSeal透かしを検査", self._detect_watermark).pack(anchor="e")
        ttk.Label(maintenance, text="バックアップ：設定・辞書・検証済み履歴。出力WAV・外部資産は別途保管してください。\n復元は現在の設定・辞書・履歴を置換します。復元前の安全バックアップを自動作成します。", wraplength=850).pack(anchor="w", pady=8)
        self._button(maintenance, "バックアップを保存", self._backup).pack(anchor="w", pady=5)
        self._button(maintenance, "バックアップから復元", self._restore).pack(anchor="w", pady=5)
        self._button(maintenance, "回復・履歴の再検査", lambda: self._start("startup", {})).pack(anchor="w", pady=5)
        for operation, label in (("install", "候補をインストール"), ("update", "候補へ更新"), ("rollback", "前の候補へ戻す"), ("recover", "アプリ更新の中断から復旧"), ("uninstall", "アプリを削除（利用者データを保持）")):
            self._button(maintenance, label, lambda operation=operation: self._lifecycle(operation)).pack(anchor="w", pady=4)
        ttk.Label(maintenance, text="データフォルダー：" + str(data_root), wraplength=850).pack(anchor="w", pady=10)
        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=(8, 0))
        self.status = tk.StringVar(value="起動時の回復を確認中…")
        ttk.Label(bottom, textvariable=self.status).pack(side="left", fill="x", expand=True)
        self.progress = ttk.Progressbar(bottom, mode="indeterminate", length=130)
        self.progress.pack(side="left", padx=8)
        self.cancel_button = ttk.Button(bottom, text="中断", command=self.worker.cancel, state="disabled")
        self.cancel_button.pack(side="right")
        window.protocol("WM_DELETE_WINDOW", self._close)
        self._start("startup", {})
        window.after(80, self._poll)

    def _button(self, parent, text, command):
        button = self.ttk.Button(parent, text=text, command=command)
        self.controls.append(button)
        return button

    def _invalidate(self):
        self.preview_ready = False
        if hasattr(self, "generate_button"):
            self.generate_button.configure(state="disabled")

    def _input_changed(self, event):
        if event.widget.edit_modified():
            self._invalidate()
            if hasattr(self, "dictionary") and event.widget == self.dictionary:
                self.settings_dirty = True
            event.widget.edit_modified(False)

    def _setting_changed(self, *_):
        self.settings_dirty = True
        self._invalidate()

    def _start(self, operation, arguments):
        if self.worker.busy:
            return
        self.current_operation = operation
        self.worker.start(operation, arguments)
        for widget in self.controls:
            widget.configure(state="disabled")
        self.text.configure(state="disabled")
        self.dictionary.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.progress.start(12)
        self.status.set({"generate": "生成中…中断後は回復処理を行います。", "preview": "トークナイザーで計画を確認中…"}.get(operation, "処理中…"))

    def _poll(self):
        if self.worker.busy and self.current_operation == "setup_inference":
            try:
                from .inference_setup import read_progress
                progress = read_progress(Path(self.worker.data_root) / "setup-progress.json")
                label = {"sources": "固定ソースを取得・構築", "wheels": "Python依存を取得・照合", "install": "専用環境へ導入", "assets": "モデル資産を取得・照合", "configure": "参照と権利資料を設定", "complete": "設定完了"}[progress["phase"]]
                self.status.set(f"{label}：{progress['completed']}/{progress['total']}　中断後は同じフォルダーで再開できます。")
            except Exception:
                pass
        try:
            operation, payload = self.worker.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.progress.stop()
            self.cancel_button.configure(state="disabled")
            for widget in self.controls:
                widget.configure(state="normal")
            self.text.configure(state="normal")
            self.dictionary.configure(state="normal")
            if payload["status"] == "cancelled":
                self._invalidate()
                self.status.set("中断しました。保存済みWAVと作業記録を再検査します。")
                self._start("startup", {})
            elif payload["status"] == "error":
                self._invalidate()
                guides = {"PREVIEW_CHANGED": "条件が変わりました。設定を保存し、再プレビューしてください。", "BUSY": "同じデータを別の処理が使用中です。終了後に再実行してください。",
                          "SETUP_DOWNLOAD": "公式ファイルを取得できませんでした。通信を確認し、初回セットアップで同じ導入先を選んで再実行してください。検査済みファイルは再利用します。"}
                self.status.set(guides.get(payload.get("code"), "処理を完了できません。設定の選択ファイル・容量・権利資料を確認し、回復・履歴の再検査から再実行してください。"))
            else:
                result = payload["result"]
                self.status.set("処理が完了しました。")
                if operation == "startup":
                    if not result["configured"]:
                        self.tabs.select(1)
                        self.status.set("保存先と設定を確認して「設定・辞書を保存」を押してください。")
                    else:
                        for key, value in result["settings"].items():
                            self.settings[key].set(value)
                        self.dictionary.delete("1.0", "end")
                        self.dictionary.insert("1.0", "\n".join(r["surface"] + "\t" + r["reading"] for r in result["dictionary"]))
                        self.dictionary.edit_modified(False)
                        self.settings_dirty = False
                        self.history.delete(0, "end")
                        self.history_ids = []
                        self.history_paths = []
                        self.history_destination.set("選択したWAVの場所をここへ表示します。")
                        for record in result["history"]:
                            self.history_ids.append(record["run_id"])
                            self.history_paths.append(str(Path(result["settings"]["output_directory"]) / record["output"]["wav_name"]))
                            from .demo_backend import DEMO_SILENCE_ONLY
                            label = "合成テスト・無音  |  " if record["backend"]["identity"] == DEMO_SILENCE_ONLY.backend_identity else ""
                            self.history.insert("end", label + record["output"]["wav_name"] + "  |  " + record["completed_at_utc"])
                elif operation == "preview":
                    self.preview_ready = True
                    self.destination.set(result["destination"])
                    p = result["preview"]
                    counts = p["plan"]
                    ranges = p["display_ranges"]
                    estimate = p["estimate"]
                    timing = estimate["generation_seconds"]
                    import math
                    time_text = "未計測" if timing is None else f"約{math.floor(timing['minimum'])}～{math.ceil(timing['maximum'])}秒（生成処理）"
                    samples = estimate["confidence"]["sample_count"]
                    note = ("同じ実行環境の成功記録がありません。音声長・所要時間は未校正です。" if not samples else
                            f"同じ環境・資産の成功{samples}件を使用。{'少数標本の参考範囲です。' if samples < 5 else '実測からの参考範囲です。'}事前検証・負荷による待ち時間は別です。")
                    self.preview_text.set(("合成テスト：無音です。\n" if result["demo"] else "実トークナイザーによる計画\n") +
                        f"入力 {counts['source_characters']}文字 / 読み変換後 {counts['synthesis_characters']}文字 / {counts['total_tokens']}トークン / {counts['chunk_count']}チャンク\n" +
                        f"生成時間：{time_text}　保存容量の目安上限：{ranges['storage_mib']['maximum']} MiB　作業容量の目安上限：{ranges['staging_mib']['maximum']} MiB\n" + note)
                elif operation == "setup_tokenizer":
                    self.settings["tokenizer_config"].set(result["tokenizer_config"])
                    self.status.set("固定版の取得・ハッシュ検証が完了しました。「設定・辞書を保存」を押してください。")
                elif operation == "setup_environment":
                    self.worker.python = result["worker_python"]
                    self.status.set("専用Python環境を作成しました。次にトークナイザーを取得し、設定を保存してください。")
                elif operation == "setup_inference":
                    self.worker.python = result["worker_python"]
                    self.settings["tokenizer_config"].set(result["tokenizer_config"])
                    self.settings["runtime_config"].set(result["runtime_config"])
                    self._invalidate()
                    self.status.set("モデル・依存・透かしの設定が完了しました。保存先を確認して「設定・辞書を保存」を押し、プレビューへ進んでください。")
                elif operation in {"prepare_references", "current_references"}:
                    self._invalidate()
                    from tkinter import messagebox
                    if result.get("configured") is False:
                        self.status.set("参照音声は未登録です。「参照音声と利用範囲を登録」から選択してください。")
                    else:
                        lines = [f"{r['frames'] / r['sample_rate']:.2f}秒 / {r['sample_rate']} Hz\n{r['path']}" for r in result["references"]]
                        messagebox.showinfo("非公開の参照音声", "\n\n".join(lines) + "\n\n生成音声の利用範囲：\n" + result["permissions"]["generated_audio_use"] + "\n\n参照の登録が完了しました。実生成には確認済みの推論環境・資産設定が別途必要です。", parent=self.window)
                        self.status.set("参照音声と許可範囲を保存・検査しました。推論資産の審査完了とは別です。")
                elif operation == "environment":
                    from tkinter import messagebox
                    messagebox.showinfo("環境確認", json.dumps(result, ensure_ascii=False, indent=2) + "\n実TTSはRTX 2060 SUPER 8 GiB・CUDA/fp32で確認しています。最低VRAMや別GPUでの動作保証ではありません。", parent=self.window)
                elif operation == "detect_watermark":
                    from tkinter import messagebox
                    messagebox.showinfo("保存WAVのAudioSeal検査", f"透かし検出のフレーム割合：{result['watermarked_frame_fraction']:.3f}\n埋め込んだ値の一致：{result['payload_matches']}\n音声長：{result['seconds']:.2f}秒　ピーク：{result['peak']:.3f}\nクリッピング：{result['clipped_samples']}サンプル\nSHA-256：{result['sha256']}\n\n検出は本人性・同意・作成元を証明しません。誤検出や改変による未検出があり得ます。", parent=self.window)
                elif operation == "generate":
                    self.destination.set(result["destination"])
                    self._invalidate()
                    self._start("startup", {})
                elif operation in {"restore", "configure", "lifecycle"}:
                    if operation == "lifecycle" and result.get("retained_root"):
                        from tkinter import messagebox
                        messagebox.showinfo("保持したアプリの場所", "以前のファイルを次の場所に保持しました。更新前へ戻すときは、このフォルダーを選択してください。\n" + result["retained_root"], parent=self.window)
                    self._invalidate()
                    self._start("startup", {})
            if not self.worker.busy:
                self.generate_button.configure(state="normal" if self.preview_ready else "disabled")
        self.window.after(80, self._poll)

    def _preview(self):
        if self.settings_dirty:
            self.status.set("変更した設定・辞書を保存してからプレビューしてください。")
            return
        self._start("preview", {"text": self.text.get("1.0", "end-1c"), "demo": self.demo.get()})

    def _generate(self):
        if self.preview_ready:
            self._start("generate", {"text": self.text.get("1.0", "end-1c"), "demo": self.demo.get()})

    def _select_setting(self, key):
        from tkinter import filedialog
        selected = filedialog.askdirectory(parent=self.window) if key == "output_directory" else filedialog.askopenfilename(parent=self.window, filetypes=[("JSON", "*.json")])
        if selected:
            self.settings[key].set(selected)

    def _setup_tokenizer(self):
        from tkinter import filedialog, messagebox
        from .setup import SOURCE, REVISION
        if not messagebox.askokcancel("取得内容の確認", "トークナイザーJSON 3件・SentencePieceデータ・MITライセンスを取得し、SHA-256を照合します。\n" + SOURCE + "\n固定版：" + REVISION + "\n推論モデル・透かし重みは取得しません。", parent=self.window):
            return
        selected = filedialog.askdirectory(parent=self.window, title="公開版専用の資産フォルダーを選択")
        if selected:
            self._start("setup_tokenizer", {"asset_directory": selected})

    def _setup_environment(self):
        from tkinter import filedialog, messagebox
        if not messagebox.askokcancel("専用環境の確認", "固定版・SHA-256付きの20依存パッケージを公式PyPIから専用環境へ導入します。\nWindows/Python 3.13用。空き700 MiBが必要です。推論モデルやPyTorchは導入しません。", parent=self.window):
            return
        selected = filedialog.askdirectory(parent=self.window, title="空の専用Python環境フォルダーを選択")
        if selected:
            self._start("setup_environment", {"environment_directory": selected})

    def _reference_setup(self):
        from .reference_dialog import ReferenceDialog
        ReferenceDialog(self)

    def _setup_inference(self):
        from .inference_dialog import InferenceDialog
        InferenceDialog(self)

    def _save(self):
        rules = []
        for line in self.dictionary.get("1.0", "end-1c").splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) != 2:
                self.status.set("辞書の表記と読みをTABで区切ってください。")
                return
            rules.append({"surface": parts[0], "reading": parts[1]})
        self._start("configure", {"values": {key: value.get() for key, value in self.settings.items()}, "rules": rules})

    def _open_text(self):
        from tkinter import filedialog
        from .local_runtime import _read
        selected = filedialog.askopenfilename(parent=self.window, filetypes=[("UTF-8 text", "*.txt")])
        if selected:
            try:
                text = _read(Path(selected).absolute(), limit=4 * 4096 + 3).decode("utf-8-sig")
                if len(text) > 4096:
                    raise ValueError()
                self.text.delete("1.0", "end")
                self.text.insert("1.0", text)
                self.destination.set("入力ファイル：" + selected)
            except Exception:
                self.status.set("UTF-8の4096文字以内のテキストを選択してください。")

    def _play(self):
        selection = self.history.curselection()
        if selection:
            self._start("play", {"run_id": self.history_ids[selection[0]]})

    def _history_selected(self, event=None):
        selection = self.history.curselection()
        if selection:
            self.history_destination.set(self.history_paths[selection[0]])

    def _detect_watermark(self):
        selection = self.history.curselection()
        if selection:
            self._start("detect_watermark", {"run_id": self.history_ids[selection[0]]})

    def _backup(self):
        from tkinter import filedialog
        selected = filedialog.asksaveasfilename(parent=self.window, defaultextension=".zip", filetypes=[("Backup", "*.zip")])
        if selected:
            self._start("backup", {"archive": selected})

    def _restore(self):
        from tkinter import filedialog, messagebox
        selected = filedialog.askopenfilename(parent=self.window, filetypes=[("Backup", "*.zip")])
        if selected and messagebox.askokcancel("復元内容の確認", "選択したバックアップで現在の設定・辞書・履歴を置換します。WAVは復元されません。\n" + selected, parent=self.window):
            self._start("restore", {"archive": selected})

    def _lifecycle(self, operation):
        from tkinter import filedialog, messagebox
        app = filedialog.askdirectory(parent=self.window, title="アプリ配置先（インストール時は新しいフォルダー名を指定）", mustexist=False)
        if not app:
            return
        layout = None
        if operation in {"install", "update", "rollback"}:
            title = "更新時に表示された、戻す保存候補を選択" if operation == "rollback" else "OWNED_FILES.jsonを含む候補を選択"
            layout = filedialog.askdirectory(parent=self.window, title=title)
            if not layout:
                return
        if messagebox.askokcancel("操作対象の確認", operation + "\n" + app + ("\n使用する候補：" + layout if layout else "") + "\n利用者のデータとWAVは保持します。", parent=self.window):
            self._start("lifecycle", {"operation": operation, "app_root": app, "layout_root": layout})

    def _close(self):
        if self.worker.busy:
            self.worker.cancel()
            self.status.set("中断完了後に閉じます。次回起動時に回復を行います。")
            self.window.after(100, self._close)
        else:
            self.window.destroy()


def main(argv=None):
    import argparse
    import tkinter as tk
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", default=str(Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "neutral-tts-workbench"))
    parser.add_argument("--worker-python", help="Python executable in the reviewed local runtime environment")
    args = parser.parse_args(argv)
    window = tk.Tk()
    Desktop(window, Path(args.data_root).absolute(), python=args.worker_python)
    window.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
