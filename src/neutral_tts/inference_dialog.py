"""Guided local inference acquisition; choices remain visible to the operator."""
from pathlib import Path


class InferenceDialog:
    def __init__(self, desktop):
        import tkinter as tk
        from tkinter import ttk, filedialog
        from .inference_setup import ROOT, catalog
        fixed = catalog()
        dialog = tk.Toplevel(desktop.window)
        dialog.title("実生成の初回セットアップ")
        dialog.geometry("830x680")
        dialog.minsize(640, 520)
        dialog.transient(desktop.window)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="参照音声と許可範囲を登録した後、公開版専用の環境を作成します。", wraplength=760).pack(anchor="w")
        from .user_guidance import show_guidance
        ttk.Button(frame, text="利用前の確認：権利・責任・個人情報・相談先", command=lambda: show_guidance(dialog)).pack(anchor="w", pady=(6, 0))
        ttk.Label(frame, text=f"モデル等：約{sum(r['size'] for r in fixed['assets']['assets']) / 1e9:.2f} GB ＋ CUDAを含む固定Python依存。\n空き16 GiBが必要です。既存のアプリ環境は選択せず、空の専用フォルダーを指定してください。\n中断時は同じフォルダーで再実行でき、取得済みファイルはハッシュを再検査します。", wraplength=760).pack(anchor="w", pady=8)
        terms_frame = ttk.Frame(frame)
        terms_frame.pack(fill="both", expand=True)
        terms = tk.Text(terms_frame, wrap="word", height=12, font=("Yu Gothic UI", 10), padx=8, pady=8)
        scroll = ttk.Scrollbar(terms_frame, command=terms.yview)
        terms.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        terms.pack(fill="both", expand=True)
        terms.insert("1.0", "参照音声と利用範囲\n"
            "録音の利用権、声の再生成への明示同意、生成音声を使える範囲を別々に確保してください。"
            "今回の登録内容を超えて公開・再配布・商用利用する許可は、このアプリから付与されません。"
            "申告は本人確認や許諾の認証ではありません。公開時は合成音声と分かる表示と文脈にしてください。\n\n"
            "利用者の責任\n権利と用途の確認は利用者が行います。利用者の無許諾・違法利用による紛争等を、"
            "開発者が代わって引き受けるものではありません。免責は適用法令の範囲内で、開発者自身の故意・重過失等の"
            "排除できない責任を免除しません。自作ソースのMIT許諾と第三者の条件は別です。\n\n"
            "TTSとトークナイザー\nIrodori-TTS-v4-Small（固定版 4c92c7ee2bb1）。MIT表記に加え、"
            "明示同意のない声の複製と、意図的に誤認させる合成音声は禁止されています。\n"
            "https://huggingface.co/Aratako/Irodori-TTS-v4-Small\n\n"
            "音声codec\nSemantic-DACVAE-Japanese-32dim（固定版 47376ee24834）。"
            "派生重みのMITと、基盤のApache-2.0の条件・表示を保持します。\n"
            "https://huggingface.co/Aratako/Semantic-DACVAE-Japanese-32dim\n\n"
            "透かし\nAudioSeal 0.2.0、固定重み 3c19eba53390（重みを含むMIT）。"
            "16-bit方式をCPUで処理します。SilentCipherとは検出方式が異なり、互換性はありません。"
            "検出は本人性・同意を証明せず、改変やなりすましを完全に防げません。\n"
            "https://github.com/facebookresearch/audioseal\n\n"
            "GPU・native依存\nCUDA 12.8とcuDNN 9.10.2系はNVIDIAの個別条件に従います。"
            "PyTorchのライセンスでGPUライブラリーの条件を置き換えることはできません。\n"
            "https://docs.nvidia.com/cuda/archive/12.8.1/eula/index.html\n"
            "https://docs.nvidia.com/deeplearning/cudnn/backend/v9.10.2/reference/eula.html\n\n"
            "取得・保存\n資産はHugging Face、ソースは公式GitHub、Python依存はPyPIとPyTorch公式配布先から取得します。"
            "固定したハッシュを照合し、ライセンス表示を保持します。"
            "参照音声や入力文を取得先へ送信する処理はありません。\n\n"
            "詳しい条件・固定版・根拠リンク：同梱 docs/INFERENCE_COMPONENTS.md\n"
            "Python/TkとGPUドライバーは別途導入が必要です。依存環境一式や音声の再配布許可は含みません。")
        terms.configure(state="disabled")
        location = tk.StringVar(value=str(Path(desktop.worker.data_root).parent / "neutral-tts-inference"))
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=8)
        ttk.Label(row, text="導入先").pack(side="left")
        ttk.Entry(row, textvariable=location).pack(side="left", fill="x", expand=True, padx=6)
        def choose():
            path = filedialog.askdirectory(parent=dialog, title="空の専用フォルダーを選択", mustexist=False)
            if path:
                location.set(path)
        ttk.Button(row, text="選択", command=choose).pack(side="right")
        device = tk.StringVar(value="cuda")
        row = ttk.Frame(frame)
        row.pack(fill="x")
        ttk.Label(row, text="TTS/codecの実行先（fp32）").pack(side="left")
        ttk.Combobox(row, textvariable=device, values=("cuda", "cpu"), state="readonly", width=12).pack(side="left", padx=8)
        ttk.Label(frame, text="CUDAは対応するNVIDIA GPUとドライバーが必要です。最低VRAM・CPUでの速度は未確定です。", wraplength=760).pack(anchor="w", pady=5)
        accepted = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="利用条件と透かし方式を確認し、登録した音声の許可範囲内で使用します。", variable=accepted).pack(anchor="w", pady=8)
        message = tk.StringVar()
        ttk.Label(frame, textvariable=message, wraplength=760).pack(anchor="w")
        def submit():
            if not accepted.get():
                message.set("利用条件と登録音声の許可範囲を確認してください。")
                return
            if not Path(location.get()).is_absolute():
                message.set("導入先を絶対パスで指定してください。")
                return
            args = {"setup_directory": location.get(), "device": device.get(), "precision": "fp32", "conditions_accepted": True}
            dialog.destroy()
            desktop._start("setup_inference", args)
        ttk.Button(frame, text="固定版を取得・検査して設定する", command=submit).pack(anchor="e", pady=(10, 0))
