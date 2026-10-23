"""Repeat a fixed, small-corpus retrieval evaluation without any model calls."""

import argparse
import hashlib
import json
from pathlib import Path
import platform
import secrets
import sys
import tempfile


REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY))

from ticket_backend import create_app  # noqa: E402
from ticket_backend.db import get_db, init_db  # noqa: E402
from ticket_backend.knowledge import import_documents, search_knowledge  # noqa: E402


def source_matches_storage(source):
    row = get_db().execute(
        "SELECT c.content, c.start_line, c.end_line, d.content AS original, d.checksum, "
        "d.id AS document_id, d.slug, d.title FROM knowledge_chunks c "
        "JOIN knowledge_documents d ON d.id=c.document_id WHERE c.id=?",
        (source["chunk_id"],),
    ).fetchone()
    if row is None:
        return False
    lines = row["original"].splitlines()
    excerpt = "\n".join(lines[row["start_line"] - 1:row["end_line"]])
    return (
        source["excerpt"] == row["content"] == excerpt
        and source["checksum"] == row["checksum"]
        == hashlib.sha256(row["original"].encode("utf-8")).hexdigest()
        and all(source[field] == row[field] for field in
                ("document_id", "slug", "title", "start_line", "end_line"))
    )


def evaluate(database):
    cases = json.loads((REPOSITORY / "examples" / "knowledge_questions.json").read_text(encoding="utf-8"))
    app = create_app({"TESTING": True, "DATABASE": str(database),
                      "SECRET_KEY": secrets.token_hex(32), "MODEL_ENABLED": False})
    results = []
    answerable, hits, top_hits, unknown, empty, checked, valid = 0, 0, 0, 0, 0, 0, 0
    with app.app_context():
        init_db()
        imported = import_documents(REPOSITORY / "examples" / "knowledge")
        for case in cases:
            response = search_knowledge(case["question"], limit=3)
            slugs = [item["slug"] for item in response["items"]]
            expected = case["expected_slugs"]
            checks = [source_matches_storage(item) for item in response["items"]]
            checked += len(checks)
            valid += sum(checks)
            if expected:
                answerable += 1
                passed = bool(set(expected) & set(slugs))
                hits += passed
                top_hits += bool(slugs and slugs[0] in expected)
            else:
                unknown += 1
                passed = not slugs and response["status"] == "insufficient_evidence"
                empty += passed
            results.append({"id": case["id"], "question": case["question"],
                            "expected_slugs": expected, "retrieved_slugs": slugs,
                            "source_checks_passed": all(checks),
                            "passed": passed and all(checks)})
    return {
        "status": "passed" if all(case["passed"] for case in results) else "failed",
        "scope": "Fixed self-authored examples; local lexical retrieval only, no model evaluation or general accuracy claim.",
        "python": platform.python_version(),
        "imported": imported,
        "metrics": {"answerable_cases": answerable, "hit_at_3": hits / answerable,
                    "hit_at_1": top_hits / answerable, "unanswerable_cases": unknown,
                    "empty_result_rate": empty / unknown,
                    "source_checks": checked, "source_checks_passed": valid,
                    "cases_passed": sum(case["passed"] for case in results), "cases_total": len(results)},
        "cases": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optional JSON result path; no secrets are written.")
    arguments = parser.parse_args()
    base = (REPOSITORY / "tests" / ".pytest-tmp").resolve()
    if not base.is_relative_to(REPOSITORY):
        raise ValueError("Temporary directory must remain inside the repository.")
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="retrieval-eval-", dir=base, ignore_cleanup_errors=True) as name:
        directory = Path(name).resolve()
        if directory.parent != base:
            raise ValueError("Unexpected temporary directory.")
        results = evaluate(directory / "evaluation.sqlite3")
    results["temporary_data_removed"] = not directory.exists()
    encoded = json.dumps(results, ensure_ascii=False, indent=2)
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0 if results["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
