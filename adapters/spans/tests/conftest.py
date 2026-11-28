"""Put the adapter dir on sys.path so the tests import it by name."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
