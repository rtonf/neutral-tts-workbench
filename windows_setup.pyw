"""Local unsigned source installer. Python/Tk must already be installed."""
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).absolute().parent / "src"))
from neutral_tts.installer import main

raise SystemExit(main())
