#!/usr/bin/env python3
"""Local-first, conservative conversation migration CLI.

The adapters intentionally avoid direct writes to live databases. Codex imports use
its app-server import protocol; Claude Code and WorkBuddy imports create new JSONL
transcripts in their native project directories.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import logging
import os
import re
import selectors
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence


LOG = logging.getLogger("agent-convoy")
SUPPORTED = ("codex", "claude", "workbuddy")
SCHEMA_VERSION = "agent-convoy/v1"
UUID_RE = re.compile(r"^[0-9a-fA-F-]{32,36}$")
SENSITIVE_KEY_RE = re.compile(r"(?i)(token|secret|password|authorization|cookie|api.?key)")


class ConvoyError(RuntimeError):
    pass


@dataclasses.dataclass
class Message:
    role: str
    content: str
    created_at: Optional[str] = None
    source_id: Optional[str] = None
    metadata: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class Conversation:
    id: str
    source: str
    title: str
    cwd: str
    created_at: Optional[str]
    updated_at: Optional[str]
    messages: list[Message]
    source_path: str
    metadata: dict[str, Any] = dataclasses.field(default_factory=dict)
    schema: str = SCHEMA_VERSION

    def digest(self) -> str:
        payload = {
            "schema": self.schema,
            "source": self.source,
            "source_id": self.id,
            "title": self.title,
            "cwd": self.cwd,
            "messages": [
                {"role": m.role, "content": m.content, "created_at": m.created_at}
                for m in self.messages
            ],
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclasses.dataclass
class SessionSummary:
    id: str
    title: str
    cwd: str
    updated_at: Optional[str]
    message_count: int
    source_path: str
    adapter: str = ""


@dataclasses.dataclass
class ImportResult:
    target_id: str
    created_paths: list[str]
    native_status: str
    details: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class Paths:
    codex_home: Path
    claude_home: Path
    workbuddy_home: Path
    workbuddy_classic_root: Path
    state_home: Path
    codex_bin: Optional[str]
    workbuddy_bin: Optional[str]


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        try:
            return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).isoformat().replace("+00:00", "Z")
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def short_text(text: str, limit: int = 80) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: max(1, limit - 1)] + "…"


def json_lines(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                LOG.debug("skip malformed JSONL record path=%s line=%d reason=%s", path, line_number, exc.msg)
                continue
            if isinstance(value, dict):
                yield value


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def ensure_private_dir(path: Path) -> None:
    created = not path.exists()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise ConvoyError(f"Refusing unsafe directory: {path}")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o022:
        if created:
            path.chmod(0o700)
        else:
            raise ConvoyError(f"Refusing group/world-writable directory: {path}")


def validate_root(path: Path, *, create: bool = False) -> Path:
    expanded = path.expanduser()
    if create and not expanded.exists():
        ensure_private_dir(expanded)
    if not expanded.exists():
        raise ConvoyError(f"Path does not exist: {expanded}")
    if expanded.is_symlink() or not expanded.is_dir():
        raise ConvoyError(f"Refusing non-directory or symlink root: {expanded}")
    owner = expanded.stat().st_uid
    if hasattr(os, "getuid") and owner != os.getuid():
        raise ConvoyError(f"Refusing directory owned by uid {owner}: {expanded}")
    return expanded.resolve()


def ensure_beneath(path: Path, root: Path) -> Path:
    resolved_root = root.resolve()
    resolved = path.resolve(strict=False)
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ConvoyError(f"Path escapes allowed root: {path}")
    return resolved


def atomic_write(path: Path, data: str, mode: int = 0o600) -> None:
    ensure_private_dir(path.parent)
    ensure_beneath(path, path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def command_version(binary: Optional[str]) -> Optional[str]:
    if not binary:
        return None
    try:
        result = subprocess.run([binary, "--version"], text=True, capture_output=True, timeout=10, check=False)
        text = (result.stdout or result.stderr).strip().splitlines()
        return text[0] if text else None
    except (OSError, subprocess.SubprocessError):
        return None


def locate_binary(name: str, candidates: Sequence[str]) -> Optional[str]:
    found = shutil.which(name)
    if found:
        return found
    for candidate in candidates:
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def default_paths(args: argparse.Namespace) -> Paths:
    home = Path.home()
    codex_home = Path(args.codex_home or os.environ.get("CODEX_HOME") or home / ".codex")
    claude_home = Path(args.claude_home or os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude")
    workbuddy_home = Path(
        args.workbuddy_home
        or os.environ.get("WORKBUDDY_CONFIG_DIR")
        or os.environ.get("CODEBUDDY_CONFIG_DIR")
        or home / ".workbuddy"
    )
    classic = Path(args.workbuddy_classic_root or home / "Library/Application Support/CodeBuddyExtension/Data")
    state_home = Path(args.state_home or home / ".agent-convoy")
    codex_bin = args.codex_bin or locate_binary(
        "codex",
        ["/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"],
    )
    workbuddy_bin = args.workbuddy_bin or locate_binary(
        "codebuddy",
        ["/Applications/WorkBuddy AI.app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy"],
    )
    return Paths(codex_home, claude_home, workbuddy_home, classic, state_home, codex_bin, workbuddy_bin)


def extract_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    pieces: list[str] = []
    for item in value:
        if isinstance(item, str):
            pieces.append(item)
            continue
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind in {"text", "input_text", "output_text"} and isinstance(item.get("text"), str):
            pieces.append(item["text"])
        elif kind == "thinking" and isinstance(item.get("thinking"), str):
            pieces.append("[Thinking]\n" + item["thinking"])
        elif kind == "tool_use":
            name = str(item.get("name") or "tool")
            tool_input = item.get("input")
            rendered = json.dumps(tool_input, ensure_ascii=False, sort_keys=True) if tool_input is not None else ""
            pieces.append(f"[Tool call: {name}]" + ("\n" + rendered if rendered else ""))
        elif kind == "tool_result":
            pieces.append("[Tool result]\n" + extract_text(item.get("content")))
    return "\n\n".join(piece for piece in pieces if piece)


def project_slug(cwd: str) -> str:
    value = str(Path(cwd).expanduser()) if cwd else str(Path.home())
    return re.sub(r"[^A-Za-z0-9_-]", "-", value)


def session_title(messages: Sequence[Message], fallback: str) -> str:
    first = next((m.content for m in messages if m.role == "user" and m.content.strip()), "")
    return short_text(first, 80) or fallback


class Adapter:
    name: str

    def list_sessions(self) -> list[SessionSummary]:
        raise NotImplementedError

    def load(self, session_id: str) -> Conversation:
        raise NotImplementedError


class ClaudeAdapter(Adapter):
    name = "claude"

    def __init__(self, home: Path):
        self.home = home.expanduser()
        self.projects = self.home / "projects"

    def _files(self) -> list[Path]:
        if not self.projects.is_dir():
            return []
        return sorted(self.projects.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)

    def _summary(self, path: Path) -> Optional[SessionSummary]:
        title = ""
        cwd = ""
        updated: Optional[str] = None
        count = 0
        session_id = path.stem
        first_user = ""
        for record in json_lines(path):
            if record.get("sessionId"):
                session_id = str(record["sessionId"])
            if record.get("cwd"):
                cwd = str(record["cwd"])
            title = str(record.get("aiTitle") or title)
            updated = parse_time(record.get("timestamp")) or updated
            if record.get("type") in {"user", "assistant"}:
                count += 1
                if not first_user and record.get("type") == "user":
                    first_user = extract_text((record.get("message") or {}).get("content"))
        if not count:
            return None
        title = title or short_text(first_user) or session_id
        return SessionSummary(session_id, title, cwd, updated, count, str(path), "claude-jsonl")

    def list_sessions(self) -> list[SessionSummary]:
        values = [summary for path in self._files() if (summary := self._summary(path))]
        return values

    def _resolve(self, session_id: str) -> Path:
        direct = [p for p in self._files() if p.stem == session_id]
        if direct:
            return direct[0]
        for path in self._files():
            for record in json_lines(path):
                if str(record.get("sessionId") or "") == session_id:
                    return path
        raise ConvoyError(f"Claude Code session not found: {session_id}")

    def load(self, session_id: str) -> Conversation:
        path = self._resolve(session_id)
        messages: list[Message] = []
        cwd = ""
        title = ""
        created: Optional[str] = None
        updated: Optional[str] = None
        actual_id = path.stem
        version = None
        for record in json_lines(path):
            actual_id = str(record.get("sessionId") or actual_id)
            cwd = str(record.get("cwd") or cwd)
            title = str(record.get("aiTitle") or title)
            version = record.get("version") or version
            timestamp = parse_time(record.get("timestamp"))
            created = created or timestamp
            updated = timestamp or updated
            kind = record.get("type")
            if kind not in {"user", "assistant"}:
                continue
            body = record.get("message") or {}
            content = extract_text(body.get("content"))
            if not content:
                continue
            role = str(body.get("role") or kind)
            raw_content = body.get("content")
            if isinstance(raw_content, list) and any(
                isinstance(item, dict) and item.get("type") == "tool_result" for item in raw_content
            ):
                role = "tool"
            messages.append(Message(role, content, timestamp, str(record.get("uuid") or "") or None))
        if not messages:
            raise ConvoyError(f"Claude session has no convertible messages: {actual_id}")
        return Conversation(actual_id, self.name, title or session_title(messages, actual_id), cwd or str(Path.home()), created, updated, messages, str(path), {"version": version})


class CodexAdapter(Adapter):
    name = "codex"

    def __init__(self, home: Path, binary: Optional[str], state_home: Path, claude_home: Optional[Path] = None):
        self.home = home.expanduser()
        self.sessions = self.home / "sessions"
        self.binary = binary
        self.state_home = state_home.expanduser()
        self.claude_home = (claude_home or (Path.home() / ".claude")).expanduser()

    def _files(self) -> list[Path]:
        if not self.sessions.is_dir():
            return []
        return sorted(self.sessions.glob("**/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)

    def _index_metadata(self) -> dict[str, dict[str, Any]]:
        db = self.home / "state_5.sqlite"
        if not db.is_file():
            return {}
        result: dict[str, dict[str, Any]] = {}
        try:
            uri = f"file:{db}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=1) as conn:
                for row in conn.execute("select id,title,cwd,updated_at_ms,preview from threads"):
                    result[str(row[0])] = {"title": row[1], "cwd": row[2], "updated": row[3], "preview": row[4]}
        except sqlite3.Error as exc:
            LOG.warning("Codex index unavailable; falling back to rollouts reason=%s", exc)
        return result

    def _probe(self, path: Path) -> Optional[tuple[str, str, str, Optional[str], int]]:
        session_id = ""
        cwd = ""
        first_user = ""
        updated = None
        count = 0
        for record in json_lines(path):
            payload = record.get("payload") or {}
            updated = parse_time(record.get("timestamp")) or updated
            if record.get("type") == "session_meta":
                session_id = str(payload.get("id") or payload.get("session_id") or session_id)
                cwd = str(payload.get("cwd") or cwd)
            if record.get("type") == "response_item" and payload.get("type") == "message":
                role = payload.get("role")
                if role in {"user", "assistant"}:
                    count += 1
                    if role == "user" and not first_user:
                        first_user = extract_text(payload.get("content"))
        if not session_id:
            match = re.search(r"([0-9a-f]{8}-[0-9a-f-]{27})", path.name, re.I)
            session_id = match.group(1) if match else ""
        return (session_id, cwd, first_user, updated, count) if session_id and count else None

    def list_sessions(self) -> list[SessionSummary]:
        index = self._index_metadata()
        values: list[SessionSummary] = []
        for path in self._files():
            probed = self._probe(path)
            if not probed:
                continue
            session_id, cwd, first_user, updated, count = probed
            meta = index.get(session_id, {})
            title = str(meta.get("title") or short_text(first_user) or session_id)
            cwd = str(meta.get("cwd") or cwd)
            updated = parse_time(meta.get("updated")) or updated
            values.append(SessionSummary(session_id, title, cwd, updated, count, str(path), "codex-rollout"))
        return values

    def _resolve(self, session_id: str) -> Path:
        for summary in self.list_sessions():
            if summary.id == session_id:
                return Path(summary.source_path)
        raise ConvoyError(f"Codex session not found: {session_id}")

    def load(self, session_id: str) -> Conversation:
        path = self._resolve(session_id)
        messages: list[Message] = []
        cwd = ""
        created = None
        updated = None
        actual_id = session_id
        for record in json_lines(path):
            payload = record.get("payload") or {}
            timestamp = parse_time(record.get("timestamp"))
            created = created or timestamp
            updated = timestamp or updated
            if record.get("type") == "session_meta":
                actual_id = str(payload.get("id") or actual_id)
                cwd = str(payload.get("cwd") or cwd)
            if record.get("type") != "response_item" or payload.get("type") != "message":
                continue
            role = str(payload.get("role") or "")
            if role not in {"user", "assistant"}:
                continue
            content = extract_text(payload.get("content"))
            if content:
                messages.append(Message(role, content, timestamp, str(payload.get("id") or "") or None))
        if not messages:
            raise ConvoyError(f"Codex session has no convertible messages: {actual_id}")
        meta = self._index_metadata().get(actual_id, {})
        return Conversation(actual_id, self.name, str(meta.get("title") or session_title(messages, actual_id)), str(meta.get("cwd") or cwd or Path.home()), created, updated, messages, str(path))

    def import_native(self, conversation: Conversation, import_id: str) -> ImportResult:
        if not self.binary:
            raise ConvoyError("Codex CLI was not found; pass --codex-bin")
        LOG.info("Codex import start source=%s messages=%d", conversation.source, len(conversation.messages))
        started = time.monotonic()
        client = CodexImportClient(self.binary)
        try:
            source_path = Path(conversation.source_path).expanduser().resolve(strict=False)
            default_claude_projects = (Path.home() / ".claude" / "projects").resolve(strict=False)
            is_native_claude_source = (
                conversation.source == "claude"
                and source_path.is_file()
                and default_claude_projects in source_path.parents
            )
            if is_native_claude_source:
                external_ledger = self.home / "external_agent_session_imports.json"
                if external_ledger.is_file():
                    try:
                        records = read_json(external_ledger).get("records", [])
                    except (OSError, json.JSONDecodeError, AttributeError):
                        records = []
                    prior = next(
                        (
                            item
                            for item in records
                            if Path(str(item.get("source_path") or "")).expanduser().resolve(strict=False) == source_path
                        ),
                        None,
                    )
                    if prior:
                        raise ConvoyError(
                            "Codex already imported this Claude session as thread "
                            + str(prior.get("imported_thread_id") or "<unknown>")
                        )
                result = client.import_session(source_path, conversation.cwd, conversation.title)
            else:
                result = client.create_thread(conversation)
                result.native_status = "codex-native-resumable-by-id-experimental"
        except Exception:
            target_id = getattr(client, "created_thread_id", None)
            if target_id and UUID_RE.fullmatch(target_id):
                cleanup = subprocess.run([self.binary, "delete", "--force", target_id], text=True, capture_output=True, timeout=30, check=False)
                LOG.warning("cleaned partial Codex import thread=%s status=%d", target_id, cleanup.returncode)
            raise
        LOG.info("Codex import complete status=%s duration_ms=%d", result.native_status, int((time.monotonic() - started) * 1000))
        return result


class WorkBuddyAdapter(Adapter):
    name = "workbuddy"

    def __init__(self, home: Path, classic_root: Path):
        self.home = home.expanduser()
        self.projects = self.home / "projects"
        self.classic_root = classic_root.expanduser()

    def _modern_files(self) -> list[Path]:
        if not self.projects.is_dir():
            return []
        return sorted(self.projects.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)

    def _classic_leaf_indexes(self) -> list[Path]:
        if not self.classic_root.is_dir():
            return []
        values: list[Path] = []
        for path in self.classic_root.glob("*/CodeBuddyIDE/*/history/*/*/index.json"):
            if (path.parent / "messages").is_dir():
                values.append(path)
        return sorted(values, key=lambda p: p.stat().st_mtime, reverse=True)

    @staticmethod
    def _classic_key(path: Path) -> str:
        digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
        return f"classic-{digest}-{path.parent.name}"

    def list_sessions(self) -> list[SessionSummary]:
        values: list[SessionSummary] = []
        for path in self._modern_files():
            adapter = ClaudeAdapter(self.home)
            summary = adapter._summary(path)
            if summary:
                summary.adapter = "workbuddy-jsonl"
                values.append(summary)
        for path in self._classic_leaf_indexes():
            try:
                leaf = read_json(path)
                root = read_json(path.parent.parent / "index.json")
            except (OSError, json.JSONDecodeError):
                continue
            convo_id = path.parent.name
            entry = next((x for x in root.get("conversations", []) if str(x.get("id")) == convo_id), {})
            title = str(entry.get("name") or convo_id)
            updated = parse_time(entry.get("lastMessageAt"))
            count = len(leaf.get("messages", []))
            values.append(SessionSummary(self._classic_key(path), title, "", updated, count, str(path), "workbuddy-classic"))
        return values

    def _load_modern(self, path: Path, requested_id: str) -> Conversation:
        conversation = ClaudeAdapter(self.home).load(requested_id)
        conversation.source = self.name
        conversation.metadata["storage"] = "workbuddy-jsonl"
        return conversation

    def _load_classic(self, path: Path, key: str) -> Conversation:
        leaf = read_json(path)
        root = read_json(path.parent.parent / "index.json")
        convo_id = path.parent.name
        entry = next((x for x in root.get("conversations", []) if str(x.get("id")) == convo_id), {})
        messages: list[Message] = []
        for item in leaf.get("messages", []):
            message_id = str(item.get("id") or "")
            msg_path = path.parent / "messages" / f"{message_id}.json"
            if not message_id or not msg_path.is_file():
                continue
            try:
                record = read_json(msg_path)
            except (OSError, json.JSONDecodeError):
                continue
            role = str(record.get("role") or item.get("role") or "")
            if role not in {"user", "assistant", "tool"}:
                continue
            content = record.get("message")
            if not isinstance(content, str) or not content:
                continue
            messages.append(Message(role, content, None, message_id))
        if not messages:
            raise ConvoyError(f"WorkBuddy classic session has no convertible messages: {key}")
        return Conversation(
            key,
            self.name,
            str(entry.get("name") or session_title(messages, convo_id)),
            str(Path.home()),
            parse_time(entry.get("createdAt")),
            parse_time(entry.get("lastMessageAt")),
            messages,
            str(path),
            {"storage": "workbuddy-classic", "conversation_id": convo_id},
        )

    def load(self, session_id: str) -> Conversation:
        for summary in self.list_sessions():
            if summary.id != session_id:
                continue
            path = Path(summary.source_path)
            if summary.adapter == "workbuddy-classic":
                return self._load_classic(path, session_id)
            return self._load_modern(path, session_id)
        raise ConvoyError(f"WorkBuddy session not found: {session_id}")


def render_claude_jsonl(conversation: Conversation, *, session_id: str, product: str) -> str:
    now = utc_now()
    version = "2.1.236" if product == "claude" else "2.156.0"
    slug = f"agent-convoy-{session_id[:8]}"
    parent: Optional[str] = None
    lines: list[str] = []
    first_user_uuid: Optional[str] = None
    for index, message in enumerate(conversation.messages):
        record_uuid = str(uuid.uuid4())
        timestamp = message.created_at or now
        role = message.role if message.role in {"user", "assistant"} else "user"
        content = message.content if message.role != "tool" else "[Tool result]\n" + message.content
        if role == "user":
            body: dict[str, Any] = {"role": "user", "content": content}
            if first_user_uuid is None:
                first_user_uuid = record_uuid
        else:
            body = {
                "id": f"msg_{uuid.uuid4().hex}",
                "type": "message",
                "role": "assistant",
                "model": "imported-history",
                "content": [{"type": "text", "text": content}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
        record = {
            "parentUuid": parent,
            "isSidechain": False,
            "userType": "external",
            "cwd": conversation.cwd,
            "sessionId": session_id,
            "session_id": session_id,
            "version": version,
            "gitBranch": "",
            "slug": slug,
            "entrypoint": "cli",
            "type": role,
            "message": body,
            "uuid": record_uuid,
            "timestamp": timestamp,
            "agentConvoy": {"schema": SCHEMA_VERSION, "source": conversation.source, "sourceId": conversation.id},
        }
        if role == "user":
            record.update(
                {
                    "origin": {"kind": "human"},
                    "promptId": str(uuid.uuid4()),
                    "promptSource": "typed",
                    "permissionMode": "auto",
                }
            )
        else:
            record["effort"] = "medium"
        lines.append(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        parent = record_uuid
    title_record = {
        "type": "ai-title",
        "aiTitle": conversation.title,
        "sessionId": session_id,
        "session_id": session_id,
        "cwd": conversation.cwd,
        "timestamp": conversation.updated_at or now,
        "uuid": str(uuid.uuid4()),
        "parentUuid": parent,
    }
    lines.append(json.dumps(title_record, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines) + "\n"


class JsonlTarget:
    def __init__(self, name: str, home: Path):
        self.name = name
        self.home = home.expanduser()
        self.projects = self.home / "projects"

    def import_native(self, conversation: Conversation) -> ImportResult:
        validate_root(self.home, create=True)
        project_dir = self.projects / project_slug(conversation.cwd)
        ensure_beneath(project_dir, self.home)
        ensure_private_dir(project_dir)
        session_id = str(uuid.uuid4())
        path = project_dir / f"{session_id}.jsonl"
        if path.exists():
            raise ConvoyError(f"Refusing to overwrite existing target: {path}")
        payload = render_claude_jsonl(conversation, session_id=session_id, product=self.name)
        LOG.info("target write start target=%s path=%s messages=%d", self.name, path, len(conversation.messages))
        atomic_write(path, payload)
        LOG.info("target write complete target=%s bytes=%d", self.name, len(payload.encode("utf-8")))
        status = "native-jsonl-experimental" if self.name == "workbuddy" else "native-jsonl-structurally-verified"
        return ImportResult(session_id, [str(path)], status)


class CodexImportClient:
    """Small JSON-RPC client for Codex's native external session importer."""

    def __init__(self, binary: str, timeout: float = 120.0):
        self.binary = binary
        self.timeout = timeout
        self.created_thread_id: Optional[str] = None

    def _write(self, proc: subprocess.Popen[str], payload: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        proc.stdin.flush()

    def delete_thread(self, thread_id: str) -> None:
        proc = subprocess.Popen(
            [self.binary, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)

        def wait_id(request_id: int, timeout: float = 20.0) -> dict[str, Any]:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if not selector.select(0.5):
                    continue
                line = proc.stdout.readline()
                if not line:
                    break
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict) and item.get("id") == request_id:
                    return item
            raise ConvoyError("Timed out waiting for Codex thread deletion")

        try:
            self._write(proc, {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "agent-convoy", "title": "Agent Convoy", "version": "0.2.0"}, "capabilities": {"experimentalApi": True}}})
            initialized = wait_id(1)
            if initialized.get("error"):
                raise ConvoyError("Codex app-server initialization failed during rollback")
            self._write(proc, {"method": "initialized", "params": {}})
            self._write(proc, {"id": 2, "method": "thread/delete", "params": {"threadId": thread_id}})
            deleted = wait_id(2)
            if deleted.get("error"):
                raise ConvoyError(
                    "Codex refused to delete the imported thread: "
                    + sanitize_diagnostic(json.dumps(deleted.get("error"), ensure_ascii=False))
                )
        finally:
            if proc.stdin:
                proc.stdin.close()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.terminate()

    def create_thread(self, conversation: Conversation) -> ImportResult:
        """Create a durable native Codex thread and inject model-visible history."""
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as errors:
            proc = subprocess.Popen(
                [self.binary, "app-server", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=errors,
                text=True,
                bufsize=1,
            )
            selector = selectors.DefaultSelector()
            assert proc.stdout is not None
            selector.register(proc.stdout, selectors.EVENT_READ)
            pending: list[dict[str, Any]] = []

            def next_message(deadline: float) -> dict[str, Any]:
                while time.monotonic() < deadline:
                    events = selector.select(max(0.0, min(0.5, deadline - time.monotonic())))
                    if not events:
                        if proc.poll() is not None:
                            break
                        continue
                    line = proc.stdout.readline()
                    if not line:
                        break
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        return value
                errors.seek(0)
                diagnostic = sanitize_diagnostic(errors.read()[-2000:])
                raise ConvoyError(f"Codex app-server did not return a usable response. {diagnostic}")

            def wait_id(request_id: int, deadline: float) -> dict[str, Any]:
                for item in list(pending):
                    if item.get("id") == request_id:
                        pending.remove(item)
                        return item
                while True:
                    item = next_message(deadline)
                    if item.get("id") == request_id:
                        return item
                    pending.append(item)

            deadline = time.monotonic() + self.timeout
            try:
                self._write(proc, {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "agent-convoy", "title": "Agent Convoy", "version": "0.2.0"}, "capabilities": {"experimentalApi": True}}})
                initialized = wait_id(1, deadline)
                if initialized.get("error"):
                    raise ConvoyError("Codex app-server initialization failed")
                self._write(proc, {"method": "initialized", "params": {}})
                requested_cwd = Path(conversation.cwd).expanduser()
                effective_cwd = str(requested_cwd.resolve()) if requested_cwd.is_dir() else str(Path.home())
                self._write(
                    proc,
                    {
                        "id": 2,
                        "method": "thread/start",
                        "params": {
                            "cwd": effective_cwd,
                            "ephemeral": False,
                            "historyMode": "paginated",
                            "threadSource": "appServer",
                        },
                    },
                )
                started = wait_id(2, deadline)
                if started.get("error"):
                    raise ConvoyError(
                        "Codex refused to create a native thread: "
                        + sanitize_diagnostic(json.dumps(started.get("error"), ensure_ascii=False))
                    )
                thread = (started.get("result") or {}).get("thread") or {}
                thread_id = str(thread.get("id") or "")
                if not UUID_RE.fullmatch(thread_id):
                    raise ConvoyError("Codex returned an invalid thread id")
                self.created_thread_id = thread_id
                items: list[dict[str, Any]] = []
                for message in conversation.messages:
                    role = message.role if message.role in {"user", "assistant"} else "user"
                    text = message.content if message.role != "tool" else "[Tool result]\n" + message.content
                    content_type = "input_text" if role == "user" else "output_text"
                    items.append(
                        {
                            "id": f"msg_{uuid.uuid4().hex}",
                            "type": "message",
                            "role": role,
                            "content": [{"type": content_type, "text": text}],
                        }
                    )
                request_id = 10
                for offset in range(0, len(items), 100):
                    self._write(
                        proc,
                        {
                            "id": request_id,
                            "method": "thread/inject_items",
                            "params": {"threadId": thread_id, "items": items[offset : offset + 100]},
                        },
                    )
                    injected = wait_id(request_id, deadline)
                    if injected.get("error"):
                        raise ConvoyError(
                            f"Codex refused history injection at message {offset + 1}: "
                            + sanitize_diagnostic(json.dumps(injected.get("error"), ensure_ascii=False))
                        )
                    request_id += 1
                self._write(proc, {"id": request_id, "method": "thread/name/set", "params": {"threadId": thread_id, "name": conversation.title}})
                named = wait_id(request_id, deadline)
                if named.get("error"):
                    raise ConvoyError("Codex created history but refused the imported title")
                return ImportResult(
                    thread_id,
                    [],
                    "codex-native-thread-injection",
                    {"effective_cwd": effective_cwd, "source_cwd": conversation.cwd},
                )
            except Exception:
                if self.created_thread_id:
                    try:
                        self._write(
                            proc,
                            {
                                "id": 9999,
                                "method": "thread/delete",
                                "params": {"threadId": self.created_thread_id},
                            },
                        )
                        cleanup = wait_id(9999, min(deadline, time.monotonic() + 10))
                        LOG.warning("native cleanup of partial Codex thread error=%s", bool(cleanup.get("error")))
                        if not cleanup.get("error"):
                            self.created_thread_id = None
                    except Exception as cleanup_exc:
                        LOG.warning("native cleanup of partial Codex thread failed reason=%s", sanitize_diagnostic(str(cleanup_exc)))
                raise
            finally:
                try:
                    if proc.stdin:
                        proc.stdin.close()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()

    def import_session(self, path: Path, cwd: str, title: str, *, claude_config_dir: Optional[Path] = None) -> ImportResult:
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as errors:
            child_env = os.environ.copy()
            if claude_config_dir is not None:
                child_env["CLAUDE_CONFIG_DIR"] = str(claude_config_dir)
            proc = subprocess.Popen(
                [self.binary, "app-server", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=errors,
                text=True,
                bufsize=1,
                env=child_env,
            )
            selector = selectors.DefaultSelector()
            assert proc.stdout is not None
            selector.register(proc.stdout, selectors.EVENT_READ)
            pending: list[dict[str, Any]] = []

            def next_message(deadline: float) -> dict[str, Any]:
                while time.monotonic() < deadline:
                    events = selector.select(max(0.0, min(0.5, deadline - time.monotonic())))
                    if not events:
                        if proc.poll() is not None:
                            break
                        continue
                    line = proc.stdout.readline()
                    if not line:
                        break
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        return value
                errors.seek(0)
                diagnostic = sanitize_diagnostic(errors.read()[-2000:])
                raise ConvoyError(f"Codex app-server did not return a usable response. {diagnostic}")

            def wait_for(predicate: Any, deadline: float) -> dict[str, Any]:
                for item in list(pending):
                    if predicate(item):
                        pending.remove(item)
                        return item
                while True:
                    item = next_message(deadline)
                    if predicate(item):
                        return item
                    pending.append(item)

            deadline = time.monotonic() + self.timeout
            try:
                self._write(proc, {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "agent-convoy", "title": "Agent Convoy", "version": "0.2.0"}, "capabilities": {"experimentalApi": True}}})
                initialized = wait_for(lambda x: x.get("id") == 1, deadline)
                if initialized.get("error"):
                    raise ConvoyError("Codex app-server initialization failed")
                self._write(proc, {"method": "initialized", "params": {}})
                self._write(
                    proc,
                    {
                        "id": 2,
                        "method": "externalAgentConfig/detect",
                        "params": {
                            "includeHome": True,
                            "cwds": [cwd],
                            "maxSessions": 1000,
                            "maxSessionAgeDays": 36500,
                            "migrationSource": "claude-code",
                        },
                    },
                )
                detected_response = wait_for(lambda x: x.get("id") == 2, deadline)
                if detected_response.get("error"):
                    raise ConvoyError("Codex external session detection failed")
                detected_items = (detected_response.get("result") or {}).get("migrationItems") or []
                selected_items = []
                for item in detected_items:
                    if item.get("itemType") != "SESSIONS":
                        continue
                    sessions = ((item.get("details") or {}).get("sessions") or [])
                    if any(Path(str(session.get("path") or "")).resolve(strict=False) == path.resolve(strict=False) for session in sessions):
                        selected_items.append(item)
                if not selected_items:
                    detected_session_count = sum(
                        len(((item.get("details") or {}).get("sessions") or []))
                        for item in detected_items
                        if item.get("itemType") == "SESSIONS"
                    )
                    raise ConvoyError(
                        "Codex did not detect the staged conversation as an importable Claude session "
                        f"(migration_items={len(detected_items)}, detected_sessions={detected_session_count})"
                    )
                params = {
                    "migrationItems": selected_items,
                    "migrationSource": "claude-code",
                    "providerId": "agent-convoy",
                    "source": "agent-convoy",
                }
                self._write(proc, {"id": 3, "method": "externalAgentConfig/import", "params": params})
                accepted = wait_for(lambda x: x.get("id") == 3, deadline)
                if accepted.get("error"):
                    raise ConvoyError("Codex rejected the external session import")
                import_id = str((accepted.get("result") or {}).get("importId") or "")
                if not import_id:
                    raise ConvoyError("Codex did not return an import id")
                completed = wait_for(
                    lambda x: x.get("method") == "externalAgentConfig/import/completed"
                    and str((x.get("params") or {}).get("importId") or "") == import_id,
                    deadline,
                )
                results = (completed.get("params") or {}).get("itemTypeResults") or []
                failures = [failure for item in results for failure in item.get("failures", [])]
                successes = [success for item in results for success in item.get("successes", [])]
                if failures or not successes:
                    reasons = sorted({str(x.get("reason") or x.get("message") or "unknown") for x in failures})
                    raise ConvoyError("Codex import failed: " + ", ".join(reasons[:3]))
                target = next((str(x.get("target")) for x in successes if x.get("target")), import_id)
                return ImportResult(target, [], "codex-native-import-protocol", {"codex_import_id": import_id})
            finally:
                try:
                    if proc.stdin:
                        proc.stdin.close()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()


def sanitize_diagnostic(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"/(?:Users|home)/[^\s\"']+", "<redacted-path>", text)
    text = re.sub(r"(?i)(bearer|token|secret|password|api.?key)[=: ]+[^\s\"']+", r"\1=<redacted>", text)
    return short_text(text, 600)


class Ledger:
    def __init__(self, home: Path):
        self.home = home.expanduser()
        self.path = self.home / "ledger.json"

    def read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"version": 2, "imports": [], "exports": []}
        try:
            value = read_json(self.path)
        except (OSError, json.JSONDecodeError) as exc:
            raise ConvoyError(f"Invalid migration ledger: {exc}") from exc
        if not isinstance(value, dict) or not isinstance(value.get("imports"), list):
            raise ConvoyError("Invalid migration ledger structure")
        # Version 1 ledgers contained imports only. Upgrade in memory and persist
        # only on the next actual mutation, preserving read-only commands.
        if not isinstance(value.get("exports"), list):
            value["exports"] = []
        value["version"] = 2
        return value

    def find_duplicate(self, target: str, digest: str) -> Optional[dict[str, Any]]:
        for record in self.read()["imports"]:
            if record.get("target") == target and record.get("digest") == digest and record.get("status") == "applied":
                return record
        return None

    def update_status(self, import_id: str, status: str) -> dict[str, Any]:
        value = self.read()
        record = next((x for x in value["imports"] if x.get("import_id") == import_id), None)
        if not record:
            raise ConvoyError(f"Import record not found: {import_id}")
        record["status"] = status
        record["updated_at"] = utc_now()
        self._save(value)
        return record

    def _save(self, value: dict[str, Any]) -> None:
        ensure_private_dir(self.home)
        if self.path.exists():
            backup_dir = self.home / "ledger-backups"
            ensure_private_dir(backup_dir)
            backup = backup_dir / f"ledger-{int(time.time() * 1000)}.json"
            shutil.copy2(self.path, backup)
            backup.chmod(0o600)
        atomic_write(self.path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def add(self, record: dict[str, Any]) -> None:
        self.add_import(record)

    def add_import(self, record: dict[str, Any]) -> None:
        value = self.read()
        value["imports"].append(record)
        self._save(value)

    def add_export(self, record: dict[str, Any]) -> None:
        value = self.read()
        value["exports"].append(record)
        self._save(value)

    def find_export(self, export_id: str) -> Optional[dict[str, Any]]:
        return next((item for item in self.read()["exports"] if item.get("export_id") == export_id), None)

    def find_export_duplicate(self, digest: str, output_dir: Path) -> Optional[dict[str, Any]]:
        resolved_dir = output_dir.resolve()
        for record in self.read()["exports"]:
            target = Path(str(record.get("path") or "")).expanduser()
            if (
                record.get("digest") == digest
                and record.get("status") == "created"
                and target.is_file()
                and target.resolve().parent == resolved_dir
            ):
                rendered_sha = record.get("rendered_sha256")
                if isinstance(rendered_sha, str):
                    actual_sha = hashlib.sha256(target.read_bytes()).hexdigest()
                    if not hmac_compare(rendered_sha, actual_sha):
                        continue
                return record
        return None


def hmac_compare(left: str, right: str) -> bool:
    # Kept here to avoid treating content hashes as ordinary string metadata.
    import hmac

    return hmac.compare_digest(left, right)


def export_filename(conversation: Conversation) -> str:
    stamp_value = conversation.updated_at or conversation.created_at or utc_now()
    try:
        stamp = dt.datetime.fromisoformat(stamp_value.replace("Z", "+00:00")).strftime("%Y%m%d-%H%M%S")
    except ValueError:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    clean_title = re.sub(r"[\x00-\x1f/:\\]+", "-", conversation.title)
    clean_title = re.sub(r"\s+", "-", clean_title).strip("-. ")[:72] or "conversation"
    short_id = re.sub(r"[^A-Za-z0-9]", "", conversation.id)[-12:] or hashlib.sha256(conversation.id.encode()).hexdigest()[:12]
    return f"{stamp}-{conversation.source}-{clean_title}-{short_id}.md"


def readable_message_content(message: Message) -> str:
    content = message.content.replace("\r\n", "\n").replace("\r", "\n").strip()
    blocks = content.split("\n\n")
    blocks = [
        block
        for block in blocks
        if not block.startswith("[Thinking]\n") and not block.startswith("[Tool call:")
    ]
    content = "\n\n".join(blocks).strip()
    content = re.sub(
        r"(?i)\b(authorization|api[-_ ]?key|access[-_ ]?token|refresh[-_ ]?token|password|secret)\b(\s*[:=]\s*)([^\s,;]+)",
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>",
        content,
    )
    content = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer <redacted>", content)
    return content


def render_markdown(conversation: Conversation) -> str:
    digest = conversation.digest()
    metadata = {
        "schema": "agent-convoy/markdown-v1",
        "source": conversation.source,
        "source_session_id": conversation.id,
        "title": conversation.title,
        "cwd": conversation.cwd,
        "created_at": conversation.created_at,
        "updated_at": conversation.updated_at,
        "content_sha256": digest,
    }
    lines = ["---"]
    for key, value in metadata.items():
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.extend(["---", "", f"# {short_text(conversation.title, 160)}", "", f"> 来源：{conversation.source} · 会话：`{conversation.id}`", ""])
    role_titles = {"user": "用户", "assistant": "助手", "tool": "工具结果", "system": "系统"}
    exported = 0
    for index, message in enumerate(conversation.messages, 1):
        content = readable_message_content(message)
        if not content:
            continue
        title = role_titles.get(message.role, message.role or "消息")
        timestamp = f" · {message.created_at}" if message.created_at else ""
        lines.extend([f"## {index}. {title}{timestamp}", "", content, ""])
        exported += 1
    if not exported:
        lines.extend(["_没有可导出的可读消息。_", ""])
    return "\n".join(lines).rstrip() + "\n"


def validate_export_directory(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.exists() or not expanded.is_dir() or expanded.is_symlink():
        raise ConvoyError(f"Export directory must be an existing non-symlink directory: {expanded}")
    resolved = expanded.resolve()
    if hasattr(os, "getuid") and resolved.stat().st_uid != os.getuid():
        raise ConvoyError(f"Export directory is not owned by the current user: {resolved}")
    if not os.access(resolved, os.W_OK):
        raise ConvoyError(f"Export directory is not writable: {resolved}")
    return resolved


def export_conversation(paths: Paths, source: str, session_id: str, output_dir: Path, *, reveal: bool = False) -> dict[str, Any]:
    conversation = adapter_for(source, paths).load(session_id)
    target_dir = validate_export_directory(output_dir)
    digest = conversation.digest()
    ledger = Ledger(paths.state_home)
    duplicate = ledger.find_export_duplicate(digest, target_dir)
    if duplicate:
        result = {**duplicate, "reused": True}
        if reveal:
            reveal_export(paths, str(duplicate.get("export_id")))
        return result
    candidate = target_dir / export_filename(conversation)
    stem, suffix = candidate.stem, candidate.suffix
    counter = 2
    while candidate.exists():
        candidate = target_dir / f"{stem}-{counter}{suffix}"
        counter += 1
    payload = render_markdown(conversation)
    rendered_sha256 = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    started = time.monotonic()
    atomic_write(candidate, payload, mode=0o600)
    export_id = str(uuid.uuid4())
    record = {
        "export_id": export_id,
        "created_at": utc_now(),
        "status": "created",
        "source": source,
        "source_id": conversation.id,
        "title": conversation.title,
        "path": str(candidate),
        "digest": digest,
        "rendered_sha256": rendered_sha256,
        "message_count": len(conversation.messages),
    }
    try:
        ledger.add_export(record)
    except Exception:
        if candidate.is_file():
            candidate.unlink()
        LOG.error("Markdown export ledger write failed; removed untracked output path=%s", candidate)
        raise
    LOG.info(
        "Markdown export complete source=%s messages=%d bytes=%d duration_ms=%d",
        source,
        len(conversation.messages),
        len(payload.encode("utf-8")),
        int((time.monotonic() - started) * 1000),
    )
    result = {**record, "reused": False}
    if reveal:
        reveal_export(paths, export_id)
    return result


def reveal_export(paths: Paths, export_id: str) -> dict[str, Any]:
    record = Ledger(paths.state_home).find_export(export_id)
    if not record or record.get("status") != "created":
        raise ConvoyError(f"Export record not found: {export_id}")
    path = Path(str(record.get("path") or "")).expanduser()
    if not path.is_file() or path.is_symlink():
        raise ConvoyError("Exported Markdown file is missing or unsafe")
    resolved = path.resolve()
    if hasattr(os, "getuid") and resolved.stat().st_uid != os.getuid():
        raise ConvoyError("Exported Markdown file is not owned by the current user")
    started = time.monotonic()
    result = subprocess.run(["/usr/bin/open", "-R", str(resolved)], text=True, capture_output=True, timeout=15, check=False)
    LOG.info("Finder reveal path=%s status=%d duration_ms=%d", resolved, result.returncode, int((time.monotonic() - started) * 1000))
    if result.returncode:
        raise ConvoyError("Finder could not reveal the exported Markdown file: " + sanitize_diagnostic(result.stderr))
    return {"export_id": export_id, "path": str(resolved), "revealed": True}


def adapter_for(name: str, paths: Paths) -> Adapter:
    if name == "codex":
        return CodexAdapter(paths.codex_home, paths.codex_bin, paths.state_home)
    if name == "claude":
        return ClaudeAdapter(paths.claude_home)
    if name == "workbuddy":
        return WorkBuddyAdapter(paths.workbuddy_home, paths.workbuddy_classic_root)
    raise ConvoyError(f"Unsupported adapter: {name}")


def filter_summaries(values: Iterable[SessionSummary], query: Optional[str], since: Optional[str]) -> list[SessionSummary]:
    result = list(values)
    if query:
        needle = query.casefold()
        result = [x for x in result if needle in x.title.casefold() or needle in x.id.casefold() or needle in x.cwd.casefold()]
    if since:
        try:
            boundary = dt.datetime.fromisoformat(since.replace("Z", "+00:00"))
            if boundary.tzinfo is None:
                boundary = boundary.replace(tzinfo=dt.timezone.utc)
        except ValueError as exc:
            raise ConvoyError("--since must be an ISO-8601 date/time") from exc
        kept = []
        for item in result:
            try:
                stamp = dt.datetime.fromisoformat((item.updated_at or "").replace("Z", "+00:00"))
            except ValueError:
                continue
            if stamp >= boundary:
                kept.append(item)
        result = kept
    return result


def choose_session(adapter: Adapter, requested: Optional[str]) -> str:
    if requested:
        return requested
    values = adapter.list_sessions()
    if not values:
        raise ConvoyError(f"No {adapter.name} sessions found")
    if not sys.stdin.isatty():
        raise ConvoyError("SESSION is required when stdin is not interactive")
    for index, item in enumerate(values[:50], 1):
        print(f"{index:2d}. {item.id}  {short_text(item.title, 70)}")
    raw = input("Select a session number: ").strip()
    try:
        choice = int(raw)
    except ValueError as exc:
        raise ConvoyError("Invalid selection") from exc
    if choice < 1 or choice > min(50, len(values)):
        raise ConvoyError("Selection out of range")
    return values[choice - 1].id


def backup_created_files(paths: Paths, import_id: str, created_paths: Sequence[str]) -> list[str]:
    backups: list[str] = []
    backup_dir = paths.state_home / "backups" / import_id
    for value in created_paths:
        source = Path(value)
        if not source.is_file():
            continue
        ensure_private_dir(backup_dir)
        target = backup_dir / f"{len(backups):03d}-{source.name}"
        shutil.copy2(source, target)
        target.chmod(0o600)
        backups.append(str(target))
    return backups


def perform_import(paths: Paths, source: str, target: str, session_id: str, *, apply: bool, title: Optional[str], cwd: Optional[str]) -> dict[str, Any]:
    if source == target:
        raise ConvoyError("Source and target must differ")
    source_adapter = adapter_for(source, paths)
    conversation = source_adapter.load(session_id)
    if title:
        conversation.title = title
    if cwd:
        conversation.cwd = str(Path(cwd).expanduser().resolve())
    digest = conversation.digest()
    ledger = Ledger(paths.state_home)
    duplicate = ledger.find_duplicate(target, digest)
    plan = {
        "source": source,
        "target": target,
        "source_id": conversation.id,
        "title": conversation.title,
        "cwd": conversation.cwd,
        "message_count": len(conversation.messages),
        "digest": digest,
        "duplicate_import_id": duplicate.get("import_id") if duplicate else None,
        "mode": "apply" if apply else "dry-run",
    }
    if duplicate:
        raise ConvoyError(f"Duplicate import detected: {duplicate.get('import_id')}")
    if not apply:
        LOG.info("dry-run source=%s target=%s messages=%d", source, target, len(conversation.messages))
        return plan
    import_id = str(uuid.uuid4())
    ensure_private_dir(paths.state_home)
    if target == "codex":
        result = CodexAdapter(paths.codex_home, paths.codex_bin, paths.state_home).import_native(conversation, import_id)
    elif target == "claude":
        result = JsonlTarget("claude", paths.claude_home).import_native(conversation)
    elif target == "workbuddy":
        result = JsonlTarget("workbuddy", paths.workbuddy_home).import_native(conversation)
    else:
        raise ConvoyError(f"Unsupported target: {target}")
    backups = backup_created_files(paths, import_id, result.created_paths)
    record = {
        "import_id": import_id,
        "created_at": utc_now(),
        "status": "applied",
        "source": source,
        "target": target,
        "source_id": conversation.id,
        "source_path": conversation.source_path,
        "target_id": result.target_id,
        "created_paths": result.created_paths,
        "backup_paths": backups,
        "digest": digest,
        "native_status": result.native_status,
        "details": result.details,
    }
    ledger.add(record)
    return {**plan, **record}


def rollback(paths: Paths, import_id: str, *, apply: bool) -> dict[str, Any]:
    ledger = Ledger(paths.state_home)
    value = ledger.read()
    record = next((x for x in value["imports"] if x.get("import_id") == import_id), None)
    if not record:
        raise ConvoyError(f"Import record not found: {import_id}")
    if record.get("status") != "applied":
        raise ConvoyError(f"Import is not active: {record.get('status')}")
    plan = {"import_id": import_id, "target": record.get("target"), "target_id": record.get("target_id"), "created_paths": record.get("created_paths", []), "mode": "apply" if apply else "dry-run"}
    if not apply:
        return plan
    target = record.get("target")
    if target == "codex":
        if not paths.codex_bin:
            raise ConvoyError("Codex CLI was not found; cannot roll back native thread")
        target_id = str(record.get("target_id") or "")
        if not UUID_RE.fullmatch(target_id):
            raise ConvoyError("Codex target id is not a safe UUID; refusing deletion")
        started = time.monotonic()
        CodexImportClient(paths.codex_bin).delete_thread(target_id)
        LOG.info("Codex rollback external call status=0 duration_ms=%d", int((time.monotonic() - started) * 1000))
    for raw_path in record.get("created_paths", []):
        path = Path(raw_path).expanduser()
        allowed_roots = [paths.state_home, paths.claude_home, paths.workbuddy_home]
        if not any(root.resolve(strict=False) == path.resolve(strict=False) or root.resolve(strict=False) in path.resolve(strict=False).parents for root in allowed_roots):
            raise ConvoyError(f"Refusing rollback outside managed roots: {path}")
        if path.is_file():
            path.unlink()
            LOG.info("rollback removed created file path=%s", path)
    ledger.update_status(import_id, "rolled_back")
    return plan


def doctor(paths: Paths) -> dict[str, Any]:
    codex = CodexAdapter(paths.codex_home, paths.codex_bin, paths.state_home)
    claude = ClaudeAdapter(paths.claude_home)
    workbuddy = WorkBuddyAdapter(paths.workbuddy_home, paths.workbuddy_classic_root)
    results: dict[str, Any] = {
        "codex": {
            "home": str(paths.codex_home),
            "exists": paths.codex_home.is_dir(),
            "version": command_version(paths.codex_bin),
            "session_count": len(codex.list_sessions()),
            "target": (
                "Claude uses native external importer; other sources use experimental native history injection (resume by id)"
                if paths.codex_bin
                else "blocked: CLI not found"
            ),
        },
        "claude": {
            "home": str(paths.claude_home),
            "exists": paths.claude_home.is_dir(),
            "versions_observed": sorted({str(c.metadata.get("version")) for s in claude.list_sessions()[:20] for c in [claude.load(s.id)] if c.metadata.get("version")}),
            "session_count": len(claude.list_sessions()),
            "target": "new native project JSONL; experimental until resumed by installed Claude Code",
        },
        "workbuddy": {
            "home": str(paths.workbuddy_home),
            "classic_root": str(paths.workbuddy_classic_root),
            "version": command_version(paths.workbuddy_bin),
            "session_count": len(workbuddy.list_sessions()),
            "modern_session_count": len(workbuddy._modern_files()),
            "classic_session_count": len(workbuddy._classic_leaf_indexes()),
            "target": "new native project JSONL; experimental",
        },
        "safety": {
            "direct_database_writes": False,
            "source_mutation": False,
            "default_mode": "dry-run",
            "ledger": str(paths.state_home / "ledger.json"),
        },
    }
    return results


def print_table(values: Sequence[SessionSummary], limit: int) -> None:
    shown = values[:limit]
    if not shown:
        print("No sessions found.")
        return
    print(f"{'ID':<52} {'UPDATED':<20} {'MSGS':>5}  TITLE")
    for item in shown:
        updated = (item.updated_at or "-")[:19]
        print(f"{item.id:<52} {updated:<20} {item.message_count:>5}  {short_text(item.title, 72)}")
    if len(values) > len(shown):
        print(f"… {len(values) - len(shown)} more")


def safe_public_record(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if not SENSITIVE_KEY_RE.search(k)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-convoy", description="Local-first conversation migration for Codex CLI, Claude Code, and WorkBuddy")
    parser.add_argument("--codex-home")
    parser.add_argument("--claude-home")
    parser.add_argument("--workbuddy-home")
    parser.add_argument("--workbuddy-classic-root")
    parser.add_argument("--state-home")
    parser.add_argument("--codex-bin")
    parser.add_argument("--workbuddy-bin")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor_cmd = sub.add_parser("doctor", help="Detect installed versions, storage, and safety status")
    doctor_cmd.add_argument("--json", action="store_true")

    list_cmd = sub.add_parser("list", help="List and filter local sessions")
    list_cmd.add_argument("source", choices=SUPPORTED)
    list_cmd.add_argument("--query")
    list_cmd.add_argument("--since")
    list_cmd.add_argument("--limit", type=int, default=30)
    list_cmd.add_argument("--json", action="store_true")

    preview_cmd = sub.add_parser("preview", help="Preview a selected session")
    preview_cmd.add_argument("source", choices=SUPPORTED)
    preview_cmd.add_argument("session")
    preview_cmd.add_argument("--messages", type=int, default=8)
    preview_cmd.add_argument("--chars", type=int, default=500)
    preview_cmd.add_argument("--json", action="store_true")

    export_cmd = sub.add_parser("export", help="Export one session to readable Markdown")
    export_cmd.add_argument("source", choices=SUPPORTED)
    export_cmd.add_argument("session")
    export_cmd.add_argument("--output-dir", required=True)
    export_cmd.add_argument("--reveal", action="store_true", help="Reveal the exported file in Finder")
    export_cmd.add_argument("--json", action="store_true")

    migrate_cmd = sub.add_parser("migrate", help="Migrate one session; dry-run unless --apply is supplied")
    migrate_cmd.add_argument("source", choices=SUPPORTED)
    migrate_cmd.add_argument("target", choices=SUPPORTED)
    migrate_cmd.add_argument("session", nargs="?")
    migrate_cmd.add_argument("--title")
    migrate_cmd.add_argument("--cwd")
    migrate_cmd.add_argument("--apply", action="store_true")
    migrate_cmd.add_argument("--json", action="store_true")

    rollback_cmd = sub.add_parser("rollback", help="Roll back one import; dry-run unless --apply is supplied")
    rollback_cmd.add_argument("import_id")
    rollback_cmd.add_argument("--apply", action="store_true")
    rollback_cmd.add_argument("--json", action="store_true")

    ledger_cmd = sub.add_parser("ledger", help="Show import history")
    ledger_cmd.add_argument("--json", action="store_true")

    ui_cmd = sub.add_parser("ui", help="Start the local Agent Convoy HTML history center")
    ui_cmd.add_argument("--port", type=int, default=0, help="Loopback port; 0 selects an available port")
    ui_cmd.add_argument("--no-open", action="store_true", help="Do not open the browser automatically")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    paths = default_paths(args)
    try:
        if args.command == "doctor":
            result = doctor(paths)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "list":
            values = filter_summaries(adapter_for(args.source, paths).list_sessions(), args.query, args.since)
            if args.json:
                print(json.dumps([dataclasses.asdict(x) for x in values[: args.limit]], ensure_ascii=False, indent=2))
            else:
                print_table(values, args.limit)
            return 0
        if args.command == "preview":
            conversation = adapter_for(args.source, paths).load(args.session)
            payload = {
                "id": conversation.id,
                "source": conversation.source,
                "title": conversation.title,
                "cwd": conversation.cwd,
                "messages": [
                    {"role": m.role, "content": m.content[: args.chars], "created_at": m.created_at}
                    for m in conversation.messages[: args.messages]
                ],
                "total_messages": len(conversation.messages),
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(f"{payload['title']}\nID: {payload['id']}\nCWD: {payload['cwd']}\nMessages: {payload['total_messages']}\n")
                for message in payload["messages"]:
                    print(f"[{message['role']}] {message['content']}\n")
            return 0
        if args.command == "export":
            result = export_conversation(paths, args.source, args.session, Path(args.output_dir), reveal=args.reveal)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "migrate":
            source_adapter = adapter_for(args.source, paths)
            selected = choose_session(source_adapter, args.session)
            result = perform_import(paths, args.source, args.target, selected, apply=args.apply, title=args.title, cwd=args.cwd)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if not args.apply:
                print("Dry-run only. Re-run with --apply to create the target session.", file=sys.stderr)
            return 0
        if args.command == "rollback":
            result = rollback(paths, args.import_id, apply=args.apply)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if not args.apply:
                print("Dry-run only. Re-run with --apply to remove the imported session.", file=sys.stderr)
            return 0
        if args.command == "ledger":
            value = Ledger(paths.state_home).read()
            payload = {
                "version": value["version"],
                "imports": [safe_public_record(x) for x in value["imports"]],
                "exports": [safe_public_record(x) for x in value["exports"]],
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        if args.command == "ui":
            if args.port < 0 or args.port > 65535:
                raise ConvoyError("--port must be between 0 and 65535")
            from agent_convoy_web import serve_ui

            serve_ui(paths, port=args.port, open_browser=not args.no_open)
            return 0
    except (ConvoyError, OSError, sqlite3.Error) as exc:
        LOG.error("%s", sanitize_diagnostic(str(exc)))
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

