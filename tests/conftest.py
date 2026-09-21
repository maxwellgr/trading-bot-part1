import sys
from pathlib import Path

# Asegura que "src" sea importable como paquete (src.xxx) sin depender de
# cómo se invoque pytest (python -m pytest vs pytest a secas, cwd distinto, etc.)
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
