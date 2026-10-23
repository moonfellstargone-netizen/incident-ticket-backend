"""Save a local DeepSeek key from hidden input or a file, without displaying it."""

import argparse
from getpass import GetPassWarning, getpass
from pathlib import Path
import sys
import tempfile
import warnings


REPOSITORY = Path(__file__).resolve().parents[1]


def _validation_error(key):
    if not key:
        return "No characters received. Use --from-file to avoid hidden terminal input."
    if len(key) > 512:
        return "More than 512 characters received. Copy only the API key."
    if "\n" in key or "\r" in key:
        return "Multiple lines received. Copy only the API key, without commands or examples."
    if any(ord(character) > 126 for character in key):
        return "Non-ASCII characters received. Remove Chinese text, full-width spaces, or curly quotes."
    if any(ord(character) < 33 for character in key):
        return "Spaces or control characters received. Use --from-file and copy only the API key."
    if len(key) >= 2 and key[0] in ('"', "'") and key[-1] == key[0]:
        return "Surrounding quotes received. Save only the API key without quotes."
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-file", type=Path, help="Read a local UTF-8 file; pass a path, never a key.")
    parser.add_argument("--replace", action="store_true", help="Explicitly replace the saved local key.")
    arguments = parser.parse_args(argv)
    destination = REPOSITORY / "instance" / "deepseek_api_key.txt"
    if destination.exists() and not arguments.replace:
        print("Local key file already exists; no changes made. Use --replace to update it.")
        return 0
    try:
        if arguments.from_file is not None:
            with arguments.from_file.open("rb") as source:
                raw = source.read(1025)
            if len(raw) > 1024:
                print("Input file exceeds 1024 bytes; nothing saved.", file=sys.stderr)
                return 1
            value = raw.decode("utf-8-sig")
        else:
            print("Input is hidden. If pasting does not work, use --from-file PATH instead.")
            with warnings.catch_warnings():
                warnings.simplefilter("error", GetPassWarning)
                value = getpass("DeepSeek API key (input hidden): ")
    except GetPassWarning:
        print("This terminal cannot hide input safely. Use --from-file with a local UTF-8 file.", file=sys.stderr)
        return 1
    except (OSError, UnicodeError, EOFError):
        print("Could not read a UTF-8 key. Use --from-file with a local UTF-8 text file.", file=sys.stderr)
        return 1
    key = value.strip()
    print(f"Received {len(key)} characters; key contents are not displayed.")
    error = _validation_error(key)
    if error is not None:
        print(error + " Nothing saved.", file=sys.stderr)
        return 1
    temporary = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if arguments.replace:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=destination.parent,
                prefix=".model-key-", suffix=".tmp", delete=False,
            ) as output:
                temporary = Path(output.name)
                output.write(key + "\n")
            temporary.replace(destination)
        else:
            with destination.open("x", encoding="utf-8") as output:
                output.write(key + "\n")
    except OSError:
        print("Could not save the local key. Check directory permissions; no key contents are shown.", file=sys.stderr)
        return 1
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    print("Saved to ignored local file instance/deepseek_api_key.txt.")
    print("Input format accepted; provider validity has not been checked.")
    print("Saving a key does not enable model calls or incur a charge.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
