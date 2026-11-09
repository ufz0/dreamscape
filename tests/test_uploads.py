"""Uploads: what a file is, where it is kept, and who may read it.

Two ways of testing the same thing. The route tests go through the app, because the size cap, the
owner check and the per-message count are what the routes enforce. The rest call the module directly,
which is what the chat and the read_file tool use.

A file is what its bytes say it is, so the fixtures are built as bytes rather than as filenames: a
.txt that is an executable is an executable.
"""
import io

import pytest

import appdata
import settings
import store
import uploads
import usage
from conftest import make_user
from test_isolation import client_for

ACTOR = {"username": "admin"}


def upload(conn, user, name, data, conversation_id=None):
    return uploads.create(conn, user, name, conversation_id, [data])


def png(width=512, height=1024):
    """A PNG header with a real IHDR, so the dimension read is a measurement, not a guess."""
    return (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" +
            int(width).to_bytes(4, "big") + int(height).to_bytes(4, "big") + b"\x08\x06\x00\x00\x00" +
            b"\x00" * 32)


def gif(width=100, height=100):
    return b"GIF89a" + int(width).to_bytes(2, "little") + int(height).to_bytes(2, "little") + b"\x00" * 20


def webp(width=600, height=600):
    chunk = b"VP8X" + b"\x00" * 4 + int(width - 1).to_bytes(3, "little") + int(height - 1).to_bytes(3, "little")
    body = b"\x00" * 20
    return b"RIFF" + int(len(body) + len(chunk) + 4).to_bytes(4, "little") + b"WEBP" + chunk + body


def jpeg(width=1024, height=512):
    """A start-of-frame marker with the dimensions in it, which is where a JPEG says its size."""
    return (b"\xff\xd8\xff\xe0" + b"\x00\x0a" + b"\x00" * 8 + b"\xff\xc0" + b"\x00\x11\x08" +
            int(height).to_bytes(2, "big") + int(width).to_bytes(2, "big") + b"\x01\x11\x00")


def pdf(text=b"hello from the pdf"):
    """A one-page PDF with a text layer. Without one pypdf reads nothing, which is what the message is about."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        None,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    content = b"BT /F1 12 Tf 20 100 Td (%s) Tj ET" % text
    objects[3] = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content)

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Root 1 0 R /Size %d >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, start)
    return bytes(out)


def docx(text="a paragraph in the document"):
    pytest.importorskip("docx")
    import docx
    document = docx.Document()
    document.add_paragraph(text)
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def test_content_decides_the_type_not_the_filename(conn):
    user = make_user(conn, "sniffuser")

    # A renamed executable: the extension claims text, the bytes say otherwise.
    with pytest.raises(uploads.UploadError) as error:
        upload(conn, user, "program.txt", b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff")
    assert "not accepted" in error.value.message
    assert uploads.used(conn, user["id"]) == 0, "a rejected file leaves nothing behind"

    # An SVG is markup: it is read as text, never sent to a model as a picture.
    svg = upload(conn, user, "diagram.svg", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>")
    assert svg["kind"] == "text" and svg["media_type"] == "image/svg+xml"

    for name, data, media, size in (("photo.png", png(), "image/png", [512, 1024]),
                                   ("photo.jpg", jpeg(), "image/jpeg", [1024, 512]),
                                   ("animation.gif", gif(), "image/gif", [100, 100]),
                                   ("photo.webp", webp(), "image/webp", [600, 600])):
        image = upload(conn, user, name, data)
        assert image["kind"] == "image" and image["media_type"] == media
        assert image["dimensions"] == size

    # An archive is not read: nothing here unpacks a zip.
    with pytest.raises(uploads.UploadError):
        upload(conn, user, "notes.zip", b"PK\x03\x04\x14\x00\x00\x00\x00\x00")
    assert uploads.sniff(conn, b"PK\x03\x04\x14\x00\x00\x00", "notes.txt") == ("other", "")


def test_a_client_filename_never_reaches_the_disk(conn):
    user = make_user(conn, "pathuser")
    saved = upload(conn, user, "../../etc/passwd.txt", b"the contents")
    row = uploads.get(conn, saved["id"])
    assert uploads.clean_name("../../etc/passwd.txt") == ".. .. etc passwd.txt", "never a path, never a control character"
    assert uploads.folder(row) == appdata.paths_for(user["id"]).uploads / saved["id"]
    assert uploads.file_path(row).name == f"{saved['id']}.txt", "the name on disk is generated"


def test_the_size_cap_is_reached_while_reading(conn):
    user = make_user(conn, "biguser")
    settings.set_value(conn, ACTOR, "max_upload_mb", "1")
    conn.commit()

    # 2 MB in small pieces: the reading passes the cap part way through, before anything is stored.
    with pytest.raises(uploads.UploadError) as error:
        uploads.create(conn, user, "large.txt", None, [b"x" * 64 * 1024] * 32)
    assert error.value.status == 413 and "1 MB per file" in error.value.message
    assert not uploads.list_for(conn, user["id"])

    # The same kind of file under the cap is kept.
    kept = uploads.create(conn, user, "large.txt", None, [b"x" * 1000])
    assert kept["size"] == 1000


def test_the_storage_quota_refuses_another_file(conn):
    user = make_user(conn, "quotauser")
    settings.set_value(conn, ACTOR, "upload_quota_mb", "1")
    conn.commit()
    upload(conn, user, "one.txt", b"x" * 900_000)
    with pytest.raises(uploads.UploadError) as error:
        upload(conn, user, "two.txt", b"x" * 900_000)
    assert error.value.status == 413 and "Storage quota reached" in error.value.message
    assert uploads.used(conn, user["id"]) == 900_000, "the refused file is not counted"

    # Deleting a file is what frees the space.
    assert uploads.delete(conn, user["id"], uploads.list_for(conn, user["id"])[0]["id"])
    assert uploads.used(conn, user["id"]) == 0


def test_a_pdf_without_a_text_layer_says_so(conn):
    user = make_user(conn, "pdfuser")
    read = upload(conn, user, "notes.pdf", pdf(b"the text layer"))
    assert read["kind"] == "pdf"
    assert uploads.extract(conn, uploads.get(conn, read["id"])) == "the text layer"

    with pytest.raises(uploads.UploadError) as error:
        upload(conn, user, "scan.pdf", pdf(b""))
    assert error.value.message == "No text layer found. Scanned PDFs are not supported."

    with pytest.raises(uploads.UploadError):
        upload(conn, user, "broken.pdf", b"%PDF-1.4\nnot a pdf at all")


def test_a_docx_is_read_as_text(conn):
    user = make_user(conn, "docxuser")
    saved = upload(conn, user, "letter.docx", docx("first paragraph"))
    row = uploads.get(conn, saved["id"])
    assert row["kind"] == "docx"
    assert uploads.extract(conn, row) == "first paragraph"


def test_a_long_file_is_inlined_only_partly_and_read_with_the_tool(conn):
    user = make_user(conn, "longuser")
    settings.set_value(conn, ACTOR, "attachment_inline_chars", "100")
    conn.commit()

    body = "x" * 500
    saved = upload(conn, user, "long.txt", body.encode())
    assert saved["text_chars"] == 100 and saved["truncated"]

    row = uploads.get(conn, saved["id"])
    block = uploads.block(conn, row, uploads.extract(conn, row))
    assert block.startswith('<attachment name="long.txt" type="text">')
    assert block.endswith("</attachment>")
    assert f'file_id "{saved["id"]}"' in block, "the block names the tool it wants used"

    # The cache holds the whole extraction, so paging past the inline cap is possible.
    first = uploads.read_text(conn, user["id"], saved["id"], 0, 100)
    assert first["chars"] == 100 and first["more"] and first["next_offset"] == 100
    second = uploads.read_text(conn, user["id"], saved["id"], first["next_offset"], 100)
    assert second["text"] == "x" * 100 and second["more"], "the rest of the file is still there"
    last = uploads.read_text(conn, user["id"], saved["id"], 400, 100)
    assert last["text"] == "x" * 100 and not last.get("more"), "the end of the file says so"
    assert uploads.read_text(conn, user["id"], "no-such-file")["error"]

    # The estimate is what the model is actually sent, which is the inline part: the rest is only
    # reached through the tool, and a tool call is a separate turn.
    assert saved["estimated_tokens"] == round(100 / usage.CHARS_PER_TOKEN) + usage.PER_MESSAGE_OVERHEAD
    settings.set_value(conn, ACTOR, "attachment_inline_chars", "1000")
    conn.commit()
    full = uploads.create(conn, user, "short.txt", None, [b"y" * 500])
    assert full["estimated_tokens"] == round(500 / usage.CHARS_PER_TOKEN) + usage.PER_MESSAGE_OVERHEAD


def test_the_model_sees_text_blocks_and_image_parts(conn):
    user = make_user(conn, "partsuser")
    text = upload(conn, user, "note.txt", b"the file says hello")
    image = upload(conn, user, "picture.png", png(100, 100))

    parts = uploads.content_parts(conn, user["id"], "what is in these?", [{"id": text["id"]}, {"id": image["id"]}])
    assert parts[0] == {"type": "text", "text": "what is in these?"}
    assert parts[1]["text"].startswith('<attachment name="note.txt"')
    assert parts[2]["type"] == "image_url" and parts[2]["image_url"]["url"].startswith("data:image/png;base64,")

    # A model that is not flagged for vision is told the file is there, and not sent the bytes.
    blocked = uploads.content_parts(conn, user["id"], "what is in this?", [{"id": image["id"]}], vision=False)
    assert blocked[1]["type"] == "text" and "cannot see images" in blocked[1]["text"]

    # An attachment that is not the user's own is not part of the message.
    other = make_user(conn, "otherparts")
    # Nothing of another account's file reaches the message, so only the text part is left.
    assert uploads.content_parts(conn, other["id"], "hello", [{"id": text["id"]}]) == [
        {"type": "text", "text": "hello"}]
    message = uploads.model_message(conn, other["id"], {"role": "user", "content": "hello",
                                                        "attachments": [{"id": text["id"]}]})
    assert message["content"] == [{"type": "text", "text": "hello"}], "nothing of it is added"
    assert "attachments" not in message, "the metadata is not part of what the model sees"


def test_only_the_owner_can_fetch_a_file(conn):
    owner = make_user(conn, "owneruser")
    admin = make_user(conn, "adminuser", role="admin")
    saved = upload(conn, owner, "private.txt", b"only mine")

    client = client_for(conn, owner)
    response = client.get(f"/api/uploads/{saved['id']}/download")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert 'attachment; filename="private.txt"' in response.headers["content-disposition"]
    assert client.get("/api/uploads").json()["files"][0]["id"] == saved["id"]

    # An admin sees numbers, not another account's files.
    admin_client = client_for(conn, admin)
    assert admin_client.get(f"/api/uploads/{saved['id']}/download").status_code == 404
    assert admin_client.get(f"/api/uploads/{saved['id']}/thumbnail").status_code == 404
    assert client.get(f"/api/uploads/{saved['id']}/thumbnail").status_code == 404, "not an image"
    assert client.get("/api/uploads/no-such-file/download").status_code == 404


def test_the_upload_route_answers_with_the_file_id(conn):
    user = make_user(conn, "routeuser")
    client = client_for(conn, user)
    response = client.post("/api/uploads", files={"file": ("hello.txt", b"a text file")})
    assert response.status_code == 200
    saved = response.json()["file"]
    assert saved["kind"] == "text" and saved["name"] == "hello.txt"

    # The same content is refused by the route when the bytes are not an accepted type.
    refused = client.post("/api/uploads", files={"file": ("program.txt", b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00")})
    assert refused.status_code == 400
    assert client.post("/api/uploads").status_code == 400, "no file is not an upload"
    assert client.delete(f"/api/uploads/{saved['id']}").json()["ok"]
    assert client.get("/api/uploads").json()["files"] == []
    assert client.delete("/api/uploads/no-such-file").status_code == 404


def test_the_size_cap_is_answered_by_the_route(conn):
    user = make_user(conn, "capuser")
    client = client_for(conn, user)
    settings.set_value(conn, ACTOR, "max_upload_mb", "1")
    conn.commit()

    # A browser announces the length before the body, so this is refused without reading any of it.
    over = client.post("/api/uploads", files={"file": ("big.txt", b"z" * 1_500_000, "text/plain")})
    assert over.status_code == 413 and "the limit is 1 MB per file" in over.json()["detail"]
    assert uploads.used(conn, user["id"]) == 0, "a refused file is not counted"

    # Under the cap the file is kept, and nothing is written before the cap is passed.
    kept = client.post("/api/uploads", files={"file": ("small.txt", b"z" * 900_000, "text/plain")})
    assert kept.status_code == 200 and uploads.used(conn, user["id"]) == 900_000
    assert kept.json()["file"]["size"] == 900_000


def test_deleting_a_chat_deletes_its_files(conn):
    user = make_user(conn, "chatuser")
    appdata.set_current(user["id"])
    appdata.current().ensure()
    cid = store.new_conversation("test-model", "")["id"]

    first = upload(conn, user, "one.txt", b"first file", conversation_id=cid)
    second = upload(conn, user, "two.txt", b"second file", conversation_id=cid)
    kept = upload(conn, user, "three.txt", b"not attached to that chat")
    folders = [uploads.folder(uploads.get(conn, item["id"])) for item in (first, second)]
    assert all(folder.exists() for folder in folders)

    client = client_for(conn, user)
    result = client.delete(f"/api/conversations/{cid}").json()
    assert result["files_removed"] == 2
    assert not any(folder.exists() for folder in folders)
    assert uploads.get(conn, kept["id"]) is not None, "a file that was not in the chat stays"
    assert len(uploads.list_for(conn, user["id"])) == 1


def test_the_storage_section_reports_usage(conn):
    user = make_user(conn, "storageuser")
    upload(conn, user, "one.txt", b"x" * 200)
    data = client_for(conn, user).get("/api/uploads").json()
    assert data["usage"]["bytes"] == 200 and data["usage"]["files"] == 1
    assert data["usage"]["quota"] == settings.number(conn, "upload_quota_mb") * 1_000_000
    assert data["usage"]["max_file"] == settings.number(conn, "max_upload_mb") * 1_000_000
    assert data["usage"]["per_message"] == settings.number(conn, "max_files_per_message")


def test_a_message_can_carry_only_so_many_files(conn):
    user = make_user(conn, "manyuser")
    appdata.set_current(user["id"])
    appdata.current().ensure()
    cid = store.new_conversation("test-model", "")["id"]
    client = client_for(conn, user)

    ids = [upload(conn, user, f"{index}.txt", b"file")["id"] for index in range(6)]
    response = client.post(f"/api/conversations/{cid}/messages", json={"message": "hello", "files": ids})
    assert response.status_code == 400 and "At most 5 files" in response.json()["detail"]

    # Another account's file id is not a file.
    other = make_user(conn, "othermany")
    foreign = upload(conn, other, "theirs.txt", b"not yours")
    response = client.post(f"/api/conversations/{cid}/messages", json={"message": "hello", "files": [foreign["id"]]})
    assert response.status_code == 404 and "No such file" in response.json()["detail"]
