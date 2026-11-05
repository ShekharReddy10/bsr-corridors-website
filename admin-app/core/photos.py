"""Photos (expense receipts, guest ID cards): checked to be an image, turned upright, shrunk to at most 1600 px and
saved as JPEG — which also drops the phone's hidden data (location, camera). Stored in Google Drive (gdrive.py)."""

import io

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_UPLOAD = 15 * 1024 * 1024
MAX_SIDE = 1600


class BadPhoto(ValueError):
    pass


def prepare(upload) -> bytes:
    if upload.size > MAX_UPLOAD:
        raise BadPhoto("That photo is too large (over 15 MB).")
    try:
        img = Image.open(upload)
        img = ImageOps.exif_transpose(img)
    except (UnidentifiedImageError, OSError) as exc:
        raise BadPhoto("That file isn’t a photo (use JPG, PNG, HEIC from the camera or gallery).") from exc
    img = img.convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=82, optimize=True)
    return out.getvalue()
