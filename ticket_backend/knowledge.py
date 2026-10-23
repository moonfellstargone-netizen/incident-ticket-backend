"""Bounded local Markdown import and explainable lexical reference retrieval.

This is a small-corpus lexical baseline, not semantic search or proof that a
matched source answers the question. A score measures ranking, not probability.
"""

from collections import Counter
import hashlib
import math
from pathlib import Path
import re

import click
from flask import Blueprint, jsonify, request
from flask.cli import with_appcontext

from .auth import require_login
from .db import get_db
from .errors import ApiError
from .validation import is_utf8_text, required_text


bp = Blueprint("knowledge", __name__)
MAX_DOCUMENTS = 50
MAX_FILE_BYTES = 128 * 1024
MAX_TOTAL_BYTES = 1024 * 1024
MAX_CHUNK_CHARS = 1200
MAX_QUERY_CHARS = 500
_MAX_ID = (1 << 63) - 1
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
_ASCII_TOKEN = re.compile(r"[a-z0-9]+(?:[_.:-][a-z0-9]+)*", re.ASCII)
_CHINESE = re.compile(r"[\u4e00-\u9fff]+")
_ENGLISH_STOP = frozenset({
    "a", "an", "and", "are", "can", "do", "does", "for", "how", "i", "in",
    "is", "it", "of", "on", "or", "please", "the", "to", "what", "when", "why",
    "with", "error", "request", "http", "example", "simulated",
})
# Split generic question/incident wording before constructing Chinese bigrams.
# This list is lexical normalization, not an inventory of supported questions.
_CHINESE_STOP = re.compile("|".join(map(re.escape, sorted({
    "为什么", "怎么办", "如何", "怎么", "什么", "请问", "出现", "遇到", "需要",
    "哪些", "能否", "是否", "怎样", "还能", "查看",
    "时候", "可能", "原因", "相关", "模拟", "故障", "问题", "处理", "排查",
    "说明", "示例", "资料", "知识", "文档", "系统", "流程", "请求", "接口",
    "返回", "请", "的", "了", "吗", "呢", "与", "和", "及", "是", "在",
}, key=lambda word: (-len(word), word)))))


def _tokens(text):
    normalized = text.lower()
    tokens = [token for token in _ASCII_TOKEN.findall(normalized)
              if len(token) > 1 and token not in _ENGLISH_STOP]
    for run in _CHINESE.findall(_CHINESE_STOP.sub(" ", normalized)):
        tokens.extend(run[index:index + 2] for index in range(len(run) - 1))
    return tokens


def _explicit_codes(text):
    # Numeric statuses and code-like ASCII identifiers must match exactly.
    # Without this guard, "Nginx 503" could pass coverage using only "Nginx"
    # and incorrectly retrieve the document explicitly scoped to status 502.
    return {
        token for token in _ASCII_TOKEN.findall(text.lower())
        if len(token) > 1 and (any(character.isdigit() for character in token) or "_" in token)
    }


def _chunk_lines(content):
    """Keep entire source lines, including blanks within each bounded chunk."""
    chunks = []
    collected = []
    start = 1
    size = 0
    for line_number, line in enumerate(content.splitlines(), 1):
        if len(line) > MAX_CHUNK_CHARS:
            raise ValueError(f"Line {line_number} exceeds {MAX_CHUNK_CHARS} characters.")
        if collected and size + len(line) + 1 > MAX_CHUNK_CHARS:
            if any(part.strip() for part in collected):
                chunks.append((start, line_number - 1, "\n".join(collected)))
            collected = []
            size = 0
        if not collected:
            start = line_number
        collected.append(line)
        size += len(line) + (1 if len(collected) > 1 else 0)
    if any(part.strip() for part in collected):
        chunks.append((start, start + len(collected) - 1, "\n".join(collected)))
    return chunks


def _read_documents(directory):
    directory = Path(directory).expanduser()
    if not directory.is_dir():
        raise ValueError("Import directory does not exist or is not a directory.")
    paths = sorted(directory.iterdir(), key=lambda path: path.name.casefold())
    paths = [path for path in paths if path.suffix.lower() == ".md"]
    if not 1 <= len(paths) <= MAX_DOCUMENTS:
        raise ValueError(f"Import 1 to {MAX_DOCUMENTS} top-level Markdown files per batch.")
    documents = []
    total_bytes = 0
    seen_slugs = set()
    for path in paths:
        # A symlink could silently import material outside the chosen directory.
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Not a regular, non-symlink Markdown file: {path.name}")
        slug = path.stem
        if not _SLUG.fullmatch(slug) or slug.casefold() in seen_slugs:
            raise ValueError("Markdown names require unique ASCII slugs (1 to 80 characters).")
        seen_slugs.add(slug.casefold())
        with path.open("rb") as source:
            raw = source.read(MAX_FILE_BYTES + 1)
        total_bytes += len(raw)
        if len(raw) > MAX_FILE_BYTES or total_bytes > MAX_TOTAL_BYTES:
            raise ValueError("Markdown file or import batch exceeds the byte limit.")
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError(f"Markdown must use UTF-8: {path.name}") from error
        if not is_utf8_text(content):
            raise ValueError(f"Markdown contains NUL or invalid text: {path.name}")
        title = next((line[2:].strip() for line in content.splitlines()
                      if line.startswith("# ")), None)
        if not title or len(title) > 120:
            raise ValueError(f"Markdown needs a '# ' title of 1 to 120 characters: {path.name}")
        chunks = _chunk_lines(content)
        documents.append((slug, title, content, hashlib.sha256(raw).hexdigest(), chunks))
    return documents


def import_documents(directory):
    """Validate first, then atomically upsert one directory; no implicit deletion.

    Run inside an application context. Missing files do not delete old documents.
    Changed documents replace their chunks; AUTOINCREMENT prevents stale source
    references from silently pointing at a newly imported chunk.
    """
    documents = _read_documents(directory)
    connection = get_db()
    statistics = {"created": 0, "updated": 0, "unchanged": 0, "chunks_written": 0}
    with connection:
        for slug, title, content, checksum, chunks in documents:
            existing = connection.execute(
                "SELECT id, checksum FROM knowledge_documents WHERE slug = ?", (slug,)
            ).fetchone()
            if existing is not None and existing["checksum"] == checksum:
                statistics["unchanged"] += 1
                continue
            if existing is None:
                cursor = connection.execute(
                    "INSERT INTO knowledge_documents (slug, title, content, checksum) VALUES (?, ?, ?, ?)",
                    (slug, title, content, checksum),
                )
                document_id = cursor.lastrowid
                statistics["created"] += 1
            else:
                document_id = existing["id"]
                connection.execute(
                    "UPDATE knowledge_documents SET title = ?, content = ?, checksum = ?, "
                    "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
                    (title, content, checksum, document_id),
                )
                connection.execute("DELETE FROM knowledge_chunks WHERE document_id = ?", (document_id,))
                statistics["updated"] += 1
            connection.executemany(
                "INSERT INTO knowledge_chunks (document_id, start_line, end_line, content) VALUES (?, ?, ?, ?)",
                [(document_id, start, end, text) for start, end, text in chunks],
            )
            statistics["chunks_written"] += len(chunks)
    return statistics


def _validated_query(query):
    return required_text(query, "q", MAX_QUERY_CHARS)


def search_knowledge(query, limit=3):
    """BM25-style chunk ranking gated by meaningful document token coverage.

    For this deliberately small corpus, require >=50% query-token coverage and
    >=2 shared tokens (or one exact ASCII term). These heuristic gates can still
    yield false matches; evaluate using held-out answerable/unanswerable queries.
    """
    query = _validated_query(query)
    if type(limit) is not int or not 1 <= limit <= 5:
        raise ApiError(400, "INVALID_REQUEST", "limit 必须是 1～5 的整数。")
    result = {"query": query, "status": "insufficient_evidence", "items": []}
    query_tokens = set(_tokens(query))
    explicit_codes = _explicit_codes(query)
    if not query_tokens:
        return result
    rows = get_db().execute(
        "SELECT c.id AS chunk_id, c.document_id, d.slug, d.title, c.content AS excerpt, "
        "c.start_line, c.end_line, d.checksum, d.content AS document_content "
        "FROM knowledge_chunks AS c JOIN knowledge_documents AS d ON d.id = c.document_id "
        "ORDER BY c.id"
    ).fetchall()
    if not rows:
        return result
    token_counts = [Counter(_tokens(row["excerpt"])) for row in rows]
    average_length = sum(sum(count.values()) for count in token_counts) / len(rows) or 1
    frequencies = Counter(token for count in token_counts for token in count)
    document_tokens = {}
    candidates = []
    for row, counts in zip(rows, token_counts):
        if row["document_id"] not in document_tokens:
            document_tokens[row["document_id"]] = set(_tokens(row["document_content"]))
        overlap = query_tokens & document_tokens[row["document_id"]]
        if not explicit_codes <= document_tokens[row["document_id"]]:
            continue
        if len(overlap) / len(query_tokens) < 0.5:
            continue
        has_exact_ascii = any(token.isascii() for token in overlap)
        if len(overlap) < 2 and not has_exact_ascii:
            continue
        chunk_overlap = query_tokens & counts.keys()
        if not chunk_overlap:
            continue
        score = 0.0
        length = sum(counts.values())
        for token in chunk_overlap:
            frequency = counts[token]
            inverse_frequency = math.log(1 + (len(rows) - frequencies[token] + 0.5) / (frequencies[token] + 0.5))
            score += inverse_frequency * frequency * 2.2 / (frequency + 1.2 * (0.25 + 0.75 * length / average_length))
        item = {key: row[key] for key in (
            "chunk_id", "document_id", "slug", "title", "excerpt", "start_line", "end_line", "checksum"
        )}
        item["score"] = round(score, 6)
        candidates.append(item)
    candidates.sort(key=lambda item: (-item["score"], item["chunk_id"]))
    result["items"] = candidates[:limit]
    if result["items"]:
        result["status"] = "found"
    return result


def _search_parameters():
    for name, values in request.args.lists():
        if name not in {"q", "limit"} or len(values) != 1:
            raise ApiError(400, "INVALID_REQUEST", "查询参数不支持或重复。")
    query = _validated_query(request.args.get("q"))
    raw_limit = request.args.get("limit", "3")
    if len(raw_limit) > 19 or not raw_limit.isascii() or not raw_limit.isdecimal():
        raise ApiError(400, "INVALID_REQUEST", "limit 必须是 1～5 的 ASCII 十进制整数。")
    limit = int(raw_limit)
    if not 1 <= limit <= 5:
        raise ApiError(400, "INVALID_REQUEST", "limit 必须是 1～5 的整数。")
    return query, limit


@bp.get("/knowledge/search")
@require_login()
def search():
    query, limit = _search_parameters()
    return jsonify(data=search_knowledge(query, limit))


@bp.get("/knowledge/documents/<document_id>")
@require_login()
def get_document(document_id):
    if (len(document_id) > 19 or not document_id.isascii()
            or not document_id.isdecimal() or not 1 <= int(document_id) <= _MAX_ID):
        raise ApiError(404, "NOT_FOUND", "参考文档不存在。")
    document = get_db().execute(
        "SELECT id, slug, title, content, checksum, updated_at FROM knowledge_documents WHERE id = ?",
        (int(document_id),),
    ).fetchone()
    if document is None:
        raise ApiError(404, "NOT_FOUND", "参考文档不存在。")
    return jsonify(data=dict(document))


@bp.get("/tickets/<ticket_id>/knowledge")
@require_login()
def ticket_knowledge(ticket_id):
    from .tickets import get_visible_ticket

    ticket = get_visible_ticket(ticket_id)
    if request.args:
        raise ApiError(400, "INVALID_REQUEST", "此接口不接受查询参数。")
    full_query = ticket["title"] + "\n" + ticket["description"]
    # The title is always retained. Long descriptions use only the first portion.
    data = search_knowledge(full_query[:MAX_QUERY_CHARS])
    data.update(ticket_id=ticket["id"], query_truncated=len(full_query) > MAX_QUERY_CHARS)
    return jsonify(data=data)


@click.command("import-knowledge")
@click.argument("directory", type=click.Path(exists=True, file_okay=False, path_type=Path))
@with_appcontext
def import_knowledge_command(directory):
    """Import a bounded top-level directory of self-authored UTF-8 Markdown."""
    try:
        statistics = import_documents(directory)
    except (ValueError, OSError) as error:
        raise click.ClickException(str(error)) from error
    click.echo("Knowledge import: " + ", ".join(f"{key}={value}" for key, value in statistics.items()))


def init_app(app):
    app.register_blueprint(bp)
    app.cli.add_command(import_knowledge_command)
