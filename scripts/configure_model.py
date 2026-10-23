"""Save a local DeepSeek key without putting it in arguments or terminal output."""

from getpass import getpass
from pathlib import Path
import sys


def main():
    destination = Path(__file__).resolve().parents[1] / "instance" / "deepseek_api_key.txt"
    if destination.exists():
        print("Local key file already exists; no changes made.")
        return 0
    key = getpass("DeepSeek API key (input hidden): ").strip()
    if not key or len(key) > 512 or any(not 33 <= ord(character) <= 126 for character in key):
        print("Invalid key format; nothing saved.", file=sys.stderr)
        return 1
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8") as output:
        output.write(key + "\n")
    print("Saved to ignored local file instance/deepseek_api_key.txt.")
    print("Saving a key does not enable model calls or incur a charge.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
