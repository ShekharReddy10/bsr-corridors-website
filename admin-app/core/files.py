"""Files people upload (guest IDs, expense receipts, resident documents): photos, PDFs, videos — or any other file.

The kind is read from the file's first bytes, never from its name or what the browser says, so a renamed file can't
pretend to be something else. Photos are turned upright, shrunk and saved as JPEG (dropping the phone's hidden data),
as before; PDFs and videos are kept as they are. Anything else is kept too, but only ever offered as a download — it
is never shown inside the app's pages (an HTML or SVG file could otherwise run in the app).
"""

from dataclasses import dataclass

from django.http import HttpResponse

from . import photos

MAX_FILE = 25 * 1024 * 1024  # per file
MAX_TOTAL = 60 * 1024 * 1024  # per upload (several files at once)
MAX_FILES = 10

KINDS = ("image", "pdf", "video", "file")


class BadFile(ValueError):
    pass


@dataclass
class Prepared:
    data: bytes
    mime: str
    ext: str
    kind: str  # image · pdf · video · file


def sniff(head: bytes) -> tuple[str, str, str]:
    """(kind, mime, extension) from a file's first bytes."""
    if head[:3] == b"\xff\xd8\xff":
        return "image", "image/jpeg", "jpg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image", "image/png", "png"
    if head[:4] == b"%PDF":
        return "pdf", "application/pdf", "pdf"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"mif1", b"msf1", b"hevc", b"heim", b"heis", b"avif"):
            return "image", "image/heic", "heic"
        if brand == b"qt  ":
            return "video", "video/quicktime", "mov"
        if brand.startswith(b"3g"):
            return "video", "video/3gpp", "3gp"
        return "video", "video/mp4", "mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "video", "video/webm", "webm"
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return "video", "video/x-msvideo", "avi"
    if head[:4] in (b"GIF8",) or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
        return "image", "image/*", "img"
    return "file", "application/octet-stream", ""


def prepare(upload) -> Prepared:
    if upload.size > MAX_FILE:
        raise BadFile(f"{upload.name} is over {MAX_FILE // 1024 // 1024} MB.")
    head = upload.read(16)
    upload.seek(0)
    kind, mime, ext = sniff(head)
    if kind == "image":
        try:
            return Prepared(photos.prepare(upload, limit=MAX_FILE), "image/jpeg", "jpg", "image")
        except photos.BadPhoto:
            if ext == "heic":  # a phone photo this server can't convert: keep it as it is (download to view)
                upload.seek(0)
                return Prepared(upload.read(), "application/octet-stream", "heic", "file")
            raise BadFile(f"{upload.name} looks like a photo but can’t be opened.")
    data = upload.read()
    if kind == "file":
        original = upload.name.rsplit(".", 1)[-1].lower() if "." in upload.name else ""
        ext = "".join(c for c in original if c.isalnum())[:8] or "bin"
    return Prepared(data, mime, ext, kind)


def check_batch(uploads) -> None:
    if len(uploads) > MAX_FILES:
        raise BadFile(f"Up to {MAX_FILES} files at a time, please.")
    if sum(u.size for u in uploads) > MAX_TOTAL:
        raise BadFile(f"These files add up to more than {MAX_TOTAL // 1024 // 1024} MB — upload them in two goes.")


def response(data: bytes, filename: str = "file") -> HttpResponse:
    """Serve a stored file: photos, PDFs and videos open in the page; anything else only downloads."""
    kind, mime, ext = sniff(data[:16])
    if kind == "file" or mime in ("image/heic", "image/*"):
        resp = HttpResponse(data, content_type="application/octet-stream")
        safe = "".join(c for c in filename if c.isalnum() or c in " ._-")[:120] or "file"
        resp["Content-Disposition"] = f'attachment; filename="{safe}"'
    else:
        resp = HttpResponse(data, content_type=mime)
        resp["Content-Disposition"] = "inline"
    resp["X-Content-Type-Options"] = "nosniff"
    resp["Cache-Control"] = "private, no-store"
    return resp
