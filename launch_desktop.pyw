"""Windows launcher; requires a separately installed Python with Tk."""
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).absolute().parent / "src"))
from neutral_tts.desktop import main

raise SystemExit(main())
