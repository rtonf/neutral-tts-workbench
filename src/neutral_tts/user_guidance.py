"""Read bundled user guidance in a local, scrollable window."""
from pathlib import Path


DOCUMENTS = {
    "権利・同意・利用者の責任": "docs/VOICE_USE.md",
    "個人情報と保存データ": "docs/PRIVACY.md",
    "権利侵害・悪用の相談": "docs/ABUSE_REPORTING.md",
    "日本法・AI指針への対応": "docs/JAPAN_AI_COMPLIANCE.md",
    "第三者部品の条件": "docs/INFERENCE_COMPONENTS.md",
    "第三者ライセンス表示": "THIRD_PARTY_NOTICES.md",
}


def show_guidance(parent):
    import tkinter as tk
    from tkinter import ttk
    previous_grab = parent.grab_current()
    window = tk.Toplevel(parent)
    window.title("利用前の確認・相談先")
    window.geometry("820x640")
    window.minsize(560, 420)
    window.transient(parent)
    frame = ttk.Frame(window, padding=12)
    frame.pack(fill="both", expand=True)
    selected = tk.StringVar(value=next(iter(DOCUMENTS)))
    selector = ttk.Combobox(frame, textvariable=selected, values=tuple(DOCUMENTS), state="readonly")
    selector.pack(fill="x", pady=(0, 8))
    ttk.Label(frame, text="同梱文書を表示します。外部への送信はありません。URLは選択してコピーできます。", wraplength=500).pack(anchor="w")
    content_frame = ttk.Frame(frame)
    content_frame.pack(fill="both", expand=True, pady=8)
    content = tk.Text(content_frame, wrap="word", font=("Yu Gothic UI", 10), padx=8, pady=8)
    scrollbar = ttk.Scrollbar(content_frame, command=content.yview)
    content.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side="right", fill="y")
    content.pack(fill="both", expand=True)

    def load(event=None):
        try:
            relative = DOCUMENTS[selected.get()]
            data = (Path(__file__).absolute().parents[2] / relative).read_bytes()
            if len(data) > 512 * 1024:
                raise ValueError("Oversized guidance")
            text = data.decode("utf-8").replace("\r\n", "\n")
        except (KeyError, OSError, UnicodeError, ValueError):
            text = "同梱の説明文書を読めません。配布ZIPのハッシュと展開内容を確認してください。"
        content.configure(state="normal")
        content.delete("1.0", "end")
        content.insert("1.0", text)
        content.configure(state="disabled")
        content.yview_moveto(0)

    def close():
        window.destroy()
        if previous_grab is not None and previous_grab.winfo_exists():
            previous_grab.grab_set()

    selector.bind("<<ComboboxSelected>>", load)
    ttk.Button(frame, text="閉じる", command=close).pack(anchor="e")
    window.protocol("WM_DELETE_WINDOW", close)
    load()
    if previous_grab is not None:
        window.grab_set()
    return window
