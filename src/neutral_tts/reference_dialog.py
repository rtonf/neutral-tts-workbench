"""Reference and permission entry; validation/copying runs in the desktop worker."""
from __future__ import annotations


class ReferenceDialog:
    def __init__(self, desktop):
        import tkinter as tk
        from tkinter import ttk, filedialog
        self.desktop = desktop
        self.window = tk.Toplevel(desktop.window)
        self.window.title("参照音声と利用範囲")
        self.window.geometry("830x660")
        self.window.transient(desktop.window)
        self.window.grab_set()
        outer = ttk.Frame(self.window, padding=15)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="参照WAVを1～4本選択（mono PCM16 / 44.1・48 kHz / 各30秒以内）").pack(anchor="w")
        self.paths = tk.Listbox(outer, height=4, exportselection=False)
        self.paths.pack(fill="x", pady=7)
        row = ttk.Frame(outer)
        row.pack(fill="x")
        def select():
            paths = filedialog.askopenfilenames(parent=self.window, filetypes=[("WAV", "*.wav")])
            if paths:
                self.paths.delete(0, "end")
                for path in paths:
                    self.paths.insert("end", path)
        ttk.Button(row, text="参照ファイルを選択", command=select).pack(side="left")
        self.evidence = tk.StringVar()
        def attach():
            selected = filedialog.askopenfilename(parent=self.window, title="許可の根拠資料（任意・256 KiB以内）")
            if selected:
                self.evidence.set(selected)
        ttk.Button(row, text="根拠資料を添付（任意）", command=attach).pack(side="left", padx=10)
        ttk.Label(outer, textvariable=self.evidence, wraplength=780).pack(anchor="w")
        self.statements, self.confirmations = {}, {}
        labels = {
            "recording_use": "録音の使用：誰から、どの試験・用途に使用する許可を得たか",
            "voice_replication": "声の再生成：話者から得た明示同意と、その範囲",
            "generated_audio_use": "生成音声：非公開試験・公開・商用など、許される用途と制限",
        }
        for scope, label in labels.items():
            ttk.Label(outer, text=label).pack(anchor="w", pady=(10, 2))
            field = tk.Text(outer, height=2, wrap="word")
            field.pack(fill="x")
            confirmed = tk.BooleanVar(value=False)
            ttk.Checkbutton(outer, text="この項目の許可範囲を確認した", variable=confirmed).pack(anchor="w")
            self.statements[scope], self.confirmations[scope] = field, confirmed
        ttk.Label(outer, text="原本を変更せず、アプリの非公開データ内に参照コピーと申告を保存します。\n申告の保存は、第三者資産のライセンス審査や音声の公開許可を代行しません。\n参照コピー・根拠資料は通常バックアップの対象外です。必要なら別途保管してください。",
                  wraplength=780).pack(anchor="w", pady=10)
        ttk.Button(outer, text="参照を検査して非公開登録", command=self.submit).pack(anchor="e")

    def submit(self):
        arguments = {
            "reference_paths": list(self.paths.get(0, "end")),
            "permissions": {k: v.get("1.0", "end-1c").strip() for k, v in self.statements.items()},
            "acknowledged": {k: v.get() for k, v in self.confirmations.items()},
            "evidence_path": self.evidence.get(),
        }
        from tkinter import messagebox
        if not 1 <= len(arguments["reference_paths"]) <= 4 or not all(arguments["acknowledged"].values()) or not all(arguments["permissions"].values()):
            messagebox.showinfo("入力の確認", "参照WAVを1～4本選び、3項目の許可範囲を入力して確認してください。", parent=self.window)
            return
        self.window.destroy()
        self.desktop._start("prepare_references", arguments)
