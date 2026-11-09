"""Files a user attaches to a message.

The bytes live in data/users/<user_id>/uploads/<uuid>/ with a generated name, and the database holds
the metadata. The extracted text is cached as a sidecar file (text.md) in the same folder, in the
same spirit as the rest of the app's plain files: a transcript stores attachment metadata only, never
base64, so the cache is what keeps a turn from re-reading the file.

What the model sees is text. An image goes as a base64 content part, and only for a model flagged for
vision; everything else is read into the cached text form and wrapped as an <attachment> block, so it
reads as data rather than as instructions. A file longer than the inline cap is included in full only
through the read_file tool, which is why the whole extraction is cached rather than a slice of it.

The size cap is met part way through a file, so an oversized one is refused before anything of it is
stored, extracted, or counted; a request that announces a length over the cap is refused before its body
is read at all. The token numbers here are estimates (the same convention as usage.py): the app cannot
know what the engine will count.
"""
import base64
import io
import math
import re
import secrets
from pathlib import Path

import appdata
import db
import settings
import usage

KINDS = ("image", "text", "pdf", "docx", "other")
TEXT_KINDS = ("text", "pdf", "docx")

# What the first bytes say the file is. Content is checked before the extension, because a filename is
# only a claim: a renamed executable is not a text file.
MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image", "image/png"),
    (b"\xff\xd8\xff", "image", "image/jpeg"),
    (b"GIF87a", "image", "image/gif"),
    (b"GIF89a", "image", "image/gif"),
    (b"RIFF", "image", "image/webp"),      # only when bytes 8:12 are WEBP
    (b"%PDF-", "pdf", "application/pdf"),
)

# A .docx is a zip. A zip that is not a docx is not read: nothing here unpacks archives.
ZIP_MAGIC = b"PK\x03\x04"

# The tile estimate an image costs a turn. An approximation of what a vision model counts.
IMAGE_TOKEN_BASE = 85
IMAGE_TOKENS_PER_TILE = 170
IMAGE_TILE = 512

# A file whose first kilobytes contain a NUL byte is binary, not text.
SNIPE_BYTES = 1024
NAME_MAX = 120


def pdf_max_pages(conn) -> int:
    return settings.number(conn, "pdf_max_pages")


class UploadError(Exception):
    """A plain reason the client can show on the chip: too large, type not allowed, no text layer."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# ---------- names and paths ----------

def clean_name(value: str) -> str:
    """The name as it may be shown. Never a path, never a control character."""
    name = str(value or "").replace("\x00", "").strip()
    name = re.sub(r"[\\/\r\n\t]+", " ", name)
    name = "".join(character for character in name if character.isprintable())
    name = re.sub(r"\s+", " ", name).strip()
    return name[:NAME_MAX] or "file"


def extension(name: str) -> str:
    return Path(str(name)).suffix.lower().lstrip(".")


def folder(row: dict) -> Path:
    """The folder for a stored file, built from the id. A client filename never reaches the disk."""
    return appdata.paths_for(row["user_id"]).uploads / str(row["id"])


def file_path(row: dict) -> Path:
    return folder(row) / str(row["stored"])


def text_path(row: dict) -> Path:
    return folder(row) / "text.md"


# ---------- what a file is ----------

def sniff(conn, head: bytes, name: str) -> tuple[str, str]:
    """(kind, media type). The bytes decide it; the extension only confirms what the bytes allow."""
    for signature, kind, media in MAGIC:
        if head.startswith(signature):
            if kind == "image" and media == "image/webp":
                if head[8:12] != b"WEBP":
                    continue
            return kind, media
    if head[:4] == ZIP_MAGIC and extension(name) == "docx":
        return "docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    # An SVG is markup, not a picture: it is read as text, never sent as an image.
    if head[:40].lstrip().lower().startswith(b"<svg"):
        return "text", "image/svg+xml"
    if head[:SNIPE_BYTES].count(b"\x00"):
        return "other", ""
    if extension(name) in settings.list_setting(conn, "upload_extensions"):
        return "text", media_for(name)
    return "other", ""


MEDIA_FOR = {
    "txt": "text/plain", "md": "text/markdown", "csv": "text/csv", "json": "application/json",
    "yaml": "text/yaml", "yml": "text/yaml", "xml": "text/xml", "html": "text/html",
    "htm": "text/html", "log": "text/plain", "svg": "image/svg+xml",
}

MEDIA_EXT = {
    "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp",
    "application/pdf": "pdf", "image/svg+xml": "svg",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
}


def media_for(name: str) -> str:
    return MEDIA_FOR.get(extension(name), "text/plain")


def allowed(conn, name: str, kind: str, media: str) -> bool:
    """The extension list is an admin setting; what the content says the file is has to match it too.

    A jpg named .png is still accepted, because the bytes are an allowed type; a renamed executable is
    not, because the bytes say it is neither text nor an accepted file.
    """
    if kind == "other":
        return False
    listed = settings.list_setting(conn, "upload_extensions")
    return extension(name) in listed or MEDIA_EXT.get(media, "") in listed


def reason_for(conn, name: str, kind: str) -> str:
    ext = extension(name) or "no extension"
    if kind == "other":
        return f"This type is not accepted: {ext}. Images, text files, PDF and docx are."
    return f"This type is not accepted: {ext}."


# ---------- storing one file ----------

def too_large_message(per_file_mb: int) -> str:
    return f"Too large: the limit is {per_file_mb} MB per file."


def create(conn, user: dict, name: str, conversation_id: str | None, chunks) -> dict:
    """Write the bytes as they arrive and stop at the cap, so an oversized file is refused early.

    `chunks` is an iterable of byte chunks, which is what the route builds from the request body. The
    file is on disk before anything is extracted, and a rejected file leaves nothing behind.
    """
    per_file = settings.number(conn, "max_upload_mb")
    limit = per_file * 1_000_000
    file_id = secrets.token_hex(16)
    target = appdata.paths_for(user["id"]).uploads / file_id
    target.mkdir(parents=True, exist_ok=True)

    head = b""
    size = 0
    kind = ""
    media = ""
    path = None
    try:
        for chunk in chunks:
            size += len(chunk)
            if size > limit:
                raise UploadError(too_large_message(per_file), 413)

            if len(head) < SNIPE_BYTES:
                head = (head + chunk)[:SNIPE_BYTES]
            if path is None:
                kind, media = sniff(conn, head, name)
                if not allowed(conn, name, kind, media):
                    raise UploadError(reason_for(conn, name, kind))
                path = target / f"{file_id}.{MEDIA_EXT.get(media) or extension(name) or kind}"
            with open(path, "ab") as handle:
                handle.write(chunk)
        if path is None or size == 0:
            raise UploadError("Nothing was uploaded.")
    except UploadError:
        remove_folder(target)
        raise

    if used(conn, user["id"]) + size > settings.number(conn, "upload_quota_mb") * 1_000_000:
        remove_folder(target)
        raise UploadError(
            f"Storage quota reached ({settings.number(conn, 'upload_quota_mb')} MB). "
            "Delete files in Settings → Storage.", 413)

    row = {"id": file_id, "user_id": user["id"], "name": clean_name(name), "stored": path.name,
          "kind": kind, "media_type": media, "size": size, "created": db.now(),
          "conversation_id": conversation_id, "text_chars": 0, "truncated": False}
    if kind in TEXT_KINDS:
        text = extract(conn, row)
        row["text_chars"] = min(len(text), settings.number(conn, "attachment_inline_chars"))
        row["truncated"] = len(text) > settings.number(conn, "attachment_inline_chars")
    row["estimated_tokens"] = estimate(row)

    conn.execute(
        "INSERT INTO uploads (id, user_id, name, stored, kind, media_type, size, created, conversation_id, "
        "text_chars, truncated, estimated_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (row["id"], row["user_id"], row["name"], row["stored"], row["kind"], row["media_type"],
         row["size"], row["created"], row["conversation_id"], row["text_chars"],
         1 if row["truncated"] else 0, row["estimated_tokens"]))
    return public(row)


def remove_folder(target: Path) -> None:
    if target.exists():
        for item in target.iterdir():
            item.unlink()
        target.rmdir()


def extract(conn, row: dict) -> str:
    """The cached extraction, or the extraction itself when it is not cached yet.

    The whole text is cached, not the inline slice, so read_file can page through the rest of a long
    file without opening the original again.
    """
    cached = text_path(row)
    if cached.exists():
        return cached.read_text(encoding="utf-8")
    text = read_content(conn, row)
    cached.write_text(text, encoding="utf-8")
    return text


def read_content(conn, row: dict) -> str:
    """A file as text the model can read. A scanned PDF has nothing to say, and that is said plainly."""
    path = file_path(row)
    kind = row["kind"]
    if kind == "pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise UploadError("PDF reading needs pypdf installed.")
        try:
            reader = PdfReader(io.BytesIO(path.read_bytes()))
        except Exception:
            raise UploadError("That PDF could not be read.")
        pages = list(reader.pages)
        lines = []
        for page in pages[:pdf_max_pages(conn)]:
            try:
                lines.append(page.extract_text() or "")
            except Exception:
                lines.append("")
        text = "\n".join(part for part in lines if part.strip()).strip()
        if not text:
            raise UploadError("No text layer found. Scanned PDFs are not supported.")
        return text
    if kind == "docx":
        try:
            import docx
        except ImportError:
            raise UploadError("docx reading needs python-docx installed.")
        try:
            document = docx.Document(path)
        except Exception:
            raise UploadError("That docx could not be read.")
        return "\n".join(paragraph.text for paragraph in document.paragraphs if paragraph.text).strip()
    return path.read_text(encoding="utf-8", errors="replace").strip()


# ---------- the cost estimate ----------

def estimate(row: dict) -> int:
    """What this file costs a turn, as an estimate: characters / 4 for text, tiles for an image."""
    if row["kind"] == "image":
        width, height = dimensions(row)
        if not width or not height:
            return IMAGE_TOKEN_BASE
        tiles = math.ceil(width / IMAGE_TILE) * math.ceil(height / IMAGE_TILE)
        return IMAGE_TOKEN_BASE + tiles * IMAGE_TOKENS_PER_TILE
    return round((row.get("text_chars") or 0) / usage.CHARS_PER_TOKEN) + usage.PER_MESSAGE_OVERHEAD


def dimensions(row: dict) -> tuple[int, int]:
    """Width and height from the file header, so the image estimate is a measurement, not a guess.

    No image library here: the sizes are read out of the header bytes directly.
    """
    raw = file_path(row).read_bytes()
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return int.from_bytes(raw[6:8], "little"), int.from_bytes(raw[8:10], "little")
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        chunk = raw[12:32]
        if chunk[:4] == b"VP8X":
            return int.from_bytes(chunk[8:11], "little") + 1, int.from_bytes(chunk[11:14], "little") + 1
        if chunk[:4] == b"VP8L":
            bits = int.from_bytes(chunk[5:9], "little")
            return (bits & 0x1FFF) + 1, ((bits >> 13) & 0x1FFF) + 1
        if chunk[:4] == b"VP8 " and len(chunk) > 10:
            return int.from_bytes(chunk[6:8], "little") & 0x3FFF, int.from_bytes(chunk[8:10], "little") & 0x3FFF
    if raw.startswith(b"\xff\xd8\xff"):
        position = 2
        while position < len(raw) - 9:
            if raw[position] != 0xFF:
                break
            marker = raw[position + 1]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                # A start-of-frame marker says height then width; the tuple here is width, height.
                return (int.from_bytes(raw[position + 7:position + 9], "big"),
                        int.from_bytes(raw[position + 5:position + 7], "big"))
            position += 2 + int.from_bytes(raw[position + 2:position + 4], "big")
    return 0, 0


def data_url(row: dict) -> str:
    return f"data:{row['media_type']};base64," + base64.b64encode(file_path(row).read_bytes()).decode("ascii")


# ---------- reading, listing, removing ----------

def get(conn, file_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM uploads WHERE id = ?", (file_id,)).fetchone()
    return dict(row) if row else None


def get_for(conn, user_id, file_id: str) -> dict | None:
    """Only the owner's own file. Another user's id, including an admin's, reads as no such file."""
    row = conn.execute("SELECT * FROM uploads WHERE id = ? AND user_id = ?", (file_id, user_id)).fetchone()
    return dict(row) if row else None


def list_for(conn, user_id) -> list[dict]:
    rows = conn.execute("SELECT * FROM uploads WHERE user_id = ? ORDER BY created DESC", (user_id,)).fetchall()
    return [public(dict(row)) for row in rows]


def used(conn, user_id) -> int:
    return int(conn.execute("SELECT COALESCE(SUM(size), 0) AS bytes FROM uploads WHERE user_id = ?",
                           (user_id,)).fetchone()["bytes"])


def delete(conn, user_id, file_id: str) -> bool:
    row = get_for(conn, user_id, file_id)
    if row is None:
        return False
    conn.execute("DELETE FROM uploads WHERE id = ?", (file_id,))
    remove_folder(folder(row))
    return True


def delete_for_conversation(conn, user_id, conversation_id: str) -> int:
    """Deleting a chat deletes the files that were attached in it."""
    removed = 0
    for row in conn.execute("SELECT * FROM uploads WHERE user_id = ? AND conversation_id = ?",
                            (user_id, conversation_id)).fetchall():
        if delete(conn, user_id, row["id"]):
            removed += 1
    return removed


def text_for(file_id: str) -> str:
    """The cached text for one file, on its own connection. This is what the dreaming job reads."""
    with db.transaction() as conn:
        row = get(conn, file_id)
        return extract(conn, row) if row else ""


def read_text(conn, user_id, file_id: str, offset: int = 0, length: int = 4000) -> dict:
    """What the read_file tool returns: a slice of the cached text, and whether there is more."""
    row = get_for(conn, user_id, file_id)
    if row is None:
        return {"error": f"no such file: {file_id}"}
    text = extract(conn, row)
    start = max(0, int(offset or 0))
    take = min(max(1, int(length or 4000)), settings.number(conn, "attachment_inline_chars"))
    part = text[start:start + take]
    result = {"name": row["name"], "kind": row["kind"], "offset": start, "chars": len(part), "text": part}
    if start + len(part) < len(text):
        result["more"] = True
        result["next_offset"] = start + len(part)
    return result


# ---------- what the model sees ----------

def metadata(conn, user_id, file_ids) -> list[dict]:
    """What a transcript stores for an attachment: id, name, kind, size. Never the file content."""
    out = []
    for file_id in file_ids or []:
        row = get_for(conn, user_id, file_id)
        if row is None:
            continue
        out.append({"id": row["id"], "name": row["name"], "kind": row["kind"],
                    "media_type": row["media_type"], "size": int(row["size"]),
                    "truncated": bool(row["truncated"]), "estimated_tokens": int(row["estimated_tokens"])})
    return out


def block(conn, row: dict, text: str) -> str:
    """An attached file as the model sees it. The delimiters are what say this is data."""
    cap = settings.number(conn, "attachment_inline_chars")
    shown = text[:cap]
    head = f'<attachment name="{row["name"]}" type="{row["kind"]}">'
    tail = "</attachment>"
    if len(text) > cap:
        tail = ('\n[The file is longer than what is shown here. Use read_file with file_id "'
                f'{row["id"]}" to read the rest.]\n{tail}')
    return f"{head}\n{shown}{tail}"


def content_parts(conn, user_id: int, text: str, attachments: list[dict], vision: bool = True) -> str | list:
    """The message content for a turn with attachments: text parts, and image parts for a vision model.

    An image for a model that is not flagged for vision is named but not sent: the model cannot see it,
    and saying so is better than sending bytes it will not read.
    """
    if not attachments:
        return text
    parts = []
    if text:
        parts.append({"type": "text", "text": text})
    for item in attachments:
        row = get(conn, item.get("id"))
        if row is None or row["user_id"] != user_id:
            continue
        if row["kind"] == "image":
            if vision:
                parts.append({"type": "image_url", "image_url": {"url": data_url(row)}})
            else:
                parts.append({"type": "text", "text": f'[Attached image "{row["name"]}" — this model '
                                                       "cannot see images, so it is not included.]"})
        else:
            parts.append({"type": "text", "text": block(conn, row, extract(conn, row))})
    return parts


def model_message(conn, user_id: int, message: dict, vision: bool = True) -> dict:
    """A message that carries attachment metadata becomes what the model sees for it.

    `store.messages()` keeps the metadata and nothing else; this is where the cached text or the image
    is put back, so a file stays part of the history on later turns.
    """
    attachments = message.get("attachments")
    if not attachments:
        return message
    out = {key: value for key, value in message.items() if key != "attachments"}
    content = content_parts(conn, user_id, message.get("content") or "", attachments, vision)
    if content:
        out["content"] = content
    return out


# ---------- what the client sees ----------

def public(row: dict) -> dict:
    return {
        "id": row["id"], "name": row["name"], "kind": row["kind"], "media_type": row["media_type"],
        "size": int(row.get("size") or 0), "created": row["created"],
        "conversation_id": row.get("conversation_id"),
        "text_chars": int(row.get("text_chars") or 0), "truncated": bool(row.get("truncated")),
        "estimated_tokens": int(row.get("estimated_tokens") or 0),
        "dimensions": list(dimensions(row)) if row.get("kind") == "image" else None,
    }


def summary(conn, user_id) -> dict:
    return {
        "bytes": used(conn, user_id),
        "files": int(conn.execute("SELECT COUNT(*) AS n FROM uploads WHERE user_id = ?",
                                 (user_id,)).fetchone()["n"]),
        "quota": settings.number(conn, "upload_quota_mb") * 1_000_000,
        "max_file": settings.number(conn, "max_upload_mb") * 1_000_000,
        "per_message": settings.number(conn, "max_files_per_message"),
        "inline_chars": settings.number(conn, "attachment_inline_chars"),
    }