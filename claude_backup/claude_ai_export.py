"""Read a claude.ai data export (Settings > Privacy > Export data).

Conversations, projects and artifacts live on Anthropic's servers, not on this
machine, so the only way to back them up is the official export. This module
turns that export into plain files: one file per artifact (its final version),
project instructions and knowledge docs, and your prompt history.
"""
import io
import json
import re
import zipfile

ARTIFACT_TOOL_NAMES = {"artifacts", "artifact", "create_artifact"}

TYPE_EXT = {
    "text/html": "html",
    "text/markdown": "md",
    "text/plain": "txt",
    "image/svg+xml": "svg",
    "application/vnd.ant.mermaid": "mmd",
    "application/vnd.ant.react": "jsx",
}

LANG_EXT = {
    "python": "py", "javascript": "js", "typescript": "ts", "jsx": "jsx", "tsx": "tsx",
    "html": "html", "css": "css", "json": "json", "bash": "sh", "shell": "sh", "sh": "sh",
    "sql": "sql", "java": "java", "c": "c", "cpp": "cpp", "c++": "cpp", "csharp": "cs",
    "go": "go", "rust": "rs", "ruby": "rb", "php": "php", "swift": "swift", "kotlin": "kt",
    "yaml": "yaml", "yml": "yaml", "xml": "xml", "markdown": "md", "r": "r", "scala": "scala",
}

LEGACY_RE = re.compile(r"<antArtifact\b([^>]*)>(.*?)</antArtifact>", re.S)
ATTR_RE = re.compile(r'(\w+)="([^"]*)"')


def slug(text, limit=60):
    s = re.sub(r"[^\w\- ]+", "", (text or "").strip()).strip().replace(" ", "-")
    return (s[:limit].strip("-") or "untitled")


def artifact_ext(kind, language):
    if kind == "application/vnd.ant.code":
        return LANG_EXT.get((language or "").lower(), "txt")
    return TYPE_EXT.get(kind or "", LANG_EXT.get((language or "").lower(), "txt"))


def _load_json_member(zf, name):
    for n in zf.namelist():
        if n.rsplit("/", 1)[-1] == name:
            with zf.open(n) as f:
                return json.load(io.TextIOWrapper(f, encoding="utf-8"))
    return None


def _message_blocks(msg):
    content = msg.get("content")
    if isinstance(content, list) and content:
        return content
    return [{"type": "text", "text": msg.get("text", "")}]


def extract_artifacts(conversations):
    """Yield (conversation, artifact dict) with each artifact at its final version."""
    for conv in conversations or []:
        state = {}
        order = []
        for msg in conv.get("chat_messages", []):
            if msg.get("sender") != "assistant":
                continue
            for block in _message_blocks(msg):
                if block.get("type") == "tool_use" and block.get("name") in ARTIFACT_TOOL_NAMES:
                    inp = block.get("input") or {}
                    aid = inp.get("id") or inp.get("identifier") or "artifact-%d" % len(order)
                    cmd = inp.get("command", "create")
                    if cmd == "update" and aid in state:
                        old, new = inp.get("old_str"), inp.get("new_str", "")
                        if old and old in state[aid]["content"]:
                            state[aid]["content"] = state[aid]["content"].replace(old, new, 1)
                        state[aid]["versions"] += 1
                        continue
                    prev = state.get(aid, {})
                    if aid not in state:
                        order.append(aid)
                    state[aid] = {
                        "id": aid,
                        "title": inp.get("title") or prev.get("title") or aid,
                        "type": inp.get("type") or prev.get("type") or "text/plain",
                        "language": inp.get("language") or prev.get("language"),
                        "content": inp.get("content") or "",
                        "versions": prev.get("versions", 0) + 1,
                        "created_at": prev.get("created_at") or msg.get("created_at"),
                    }
                elif block.get("type") == "text":
                    for m in LEGACY_RE.finditer(block.get("text") or ""):
                        attrs = dict(ATTR_RE.findall(m.group(1)))
                        aid = attrs.get("identifier") or "artifact-%d" % len(order)
                        prev = state.get(aid, {})
                        if aid not in state:
                            order.append(aid)
                        state[aid] = {
                            "id": aid,
                            "title": attrs.get("title") or aid,
                            "type": attrs.get("type") or "text/plain",
                            "language": attrs.get("language"),
                            "content": m.group(2).strip("\n"),
                            "versions": prev.get("versions", 0) + 1,
                            "created_at": prev.get("created_at") or msg.get("created_at"),
                        }
        for aid in order:
            yield conv, state[aid]


def read_export(path):
    """Return a list of (relative_path, bytes, category) built from an export zip."""
    out = []
    with zipfile.ZipFile(path) as zf:
        conversations = _load_json_member(zf, "conversations.json") or []
        projects = _load_json_member(zf, "projects.json") or []
        memories = _load_json_member(zf, "memories.json")

    # Artifacts --------------------------------------------------------------
    index = []
    used = set()
    for conv, art in extract_artifacts(conversations):
        date = (conv.get("created_at") or "")[:10] or "undated"
        folder = "%s_%s" % (date, slug(conv.get("name") or conv.get("uuid", "")[:8], 40))
        name = "%s.%s" % (slug(art["title"]), artifact_ext(art["type"], art["language"]))
        rel = "%s/%s" % (folder, name)
        n = 2
        while rel in used:
            rel = "%s/%s-%d.%s" % (folder, name.rsplit(".", 1)[0], n, name.rsplit(".", 1)[1])
            n += 1
        used.add(rel)
        out.append((rel, art["content"].encode("utf-8"), "artifacts"))
        index.append({
            "file": rel, "title": art["title"], "type": art["type"],
            "language": art["language"], "versions": art["versions"],
            "conversation": conv.get("name"), "conversation_uuid": conv.get("uuid"),
            "created_at": art["created_at"],
        })
    if index:
        out.append(("index.json", json.dumps(index, indent=2).encode("utf-8"), "artifacts"))

    # Prompts: project instructions + knowledge, and every message you sent --
    for p in projects:
        base = "projects/%s" % slug(p.get("name") or p.get("uuid", ""))
        if p.get("prompt_template"):
            out.append(("%s/instructions.md" % base, p["prompt_template"].encode("utf-8"), "prompts"))
        if p.get("description"):
            out.append(("%s/description.md" % base, p["description"].encode("utf-8"), "prompts"))
        for d in p.get("docs") or []:
            fname = d.get("filename") or "doc-%s.md" % (d.get("uuid") or "")[:8]
            out.append(("%s/knowledge/%s" % (base, fname.replace("/", "_")),
                        (d.get("content") or "").encode("utf-8"), "prompts"))
    lines = []
    for conv in conversations:
        for msg in conv.get("chat_messages", []):
            if msg.get("sender") == "human":
                text = msg.get("text") or "".join(
                    b.get("text", "") for b in _message_blocks(msg) if b.get("type") == "text")
                if text.strip():
                    lines.append(json.dumps({
                        "conversation": conv.get("name"), "conversation_uuid": conv.get("uuid"),
                        "created_at": msg.get("created_at"), "prompt": text,
                    }, ensure_ascii=False))
    if lines:
        out.append(("prompt-history.jsonl", ("\n".join(lines) + "\n").encode("utf-8"), "prompts"))
    if memories is not None:
        out.append(("memories.json", json.dumps(memories, indent=2).encode("utf-8"), "prompts"))
    if conversations:
        out.append(("conversations.json", json.dumps(conversations, ensure_ascii=False).encode("utf-8"),
                    "history"))
    return out
