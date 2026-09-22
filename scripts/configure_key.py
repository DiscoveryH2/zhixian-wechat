"""Accept a key through standard input without echoing or logging it."""
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from desk.store import Store

key = getpass.getpass('API key (hidden): ') if sys.stdin.isatty() else sys.stdin.readline().strip()
if not key:
    raise SystemExit('No credential supplied.')
Store(ROOT / 'data').save_config({'api_key': key, 'base_url': 'https://openrouter.ai/api', 'model_name': 'typesafe/jev-1.13'})
print('Credential encrypted and saved locally.')
