import io

from PIL import Image

# iPhone photos (owner, 11/05: a photo from the phone wouldn't go into the chat). Two things Apple
# sends that plain JPEG / PNG checks turned away: HEIC files, and JPGs that carry extra layers (depth,
# HDR), which Pillow reports as "MPO". Both are ordinary photos.
try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:  # pragma: no cover - only without the package installed
    pass

_JPEG_KINDS = ("JPEG", "MPO")
_MAX_PIXELS = 120_000_000


def assert_valid_jpeg(contents: bytes) -> None:
    """Confirms `contents` actually decode as a JPEG, not just that the client claimed a JPEG
    Content-Type header - that header is fully attacker-controlled and was, until now, the only
    check any upload path in this app performed. Raises ValueError (caller turns this into a
    422) on anything that isn't a genuine, intact JPEG."""
    try:
        image = Image.open(io.BytesIO(contents))
        image.verify()
    except Exception:
        raise ValueError("File is not a valid JPEG image.")

    if image.format not in _JPEG_KINDS:
        raise ValueError("File is not a valid JPEG image.")


def photo_to_jpeg(contents: bytes) -> bytes:
    """AI Media uploads: accepts a genuine JPEG, PNG, WebP or iPhone HEIC photo and returns JPEG bytes -
    the format every AI Media step expects. A JPEG passes through untouched (no re-compression);
    anything else is turned upright (EXIF), any see-through areas laid on white, and saved as a
    high-quality JPEG. Raises ValueError on anything that isn't a real image in those formats."""
    try:
        probe = Image.open(io.BytesIO(contents))
        probe.verify()
    except Exception:
        raise ValueError("File is not a valid image.")
    # A small file can open up to something enormous; a 48MP iPhone photo is well under this.
    if probe.size[0] * probe.size[1] > _MAX_PIXELS:
        raise ValueError("That photo is too large.")
    if probe.format == "JPEG":
        return contents
    if probe.format not in ("PNG", "WEBP", "MPO", "HEIF"):
        raise ValueError("Only JPG, PNG, WebP or iPhone photos are supported.")
    from PIL import ImageOps
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(contents)))
    if image.mode in ("RGBA", "LA", "P"):
        image = image.convert("RGBA")
        flat = Image.new("RGB", image.size, (255, 255, 255))
        flat.paste(image, mask=image.split()[-1])
        image = flat
    else:
        image = image.convert("RGB")
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=95, subsampling=0, optimize=True)
    return out.getvalue()


def headshot_jpeg(contents: bytes, max_side: int = 2000) -> bytes:
    """Every agent headshot, wherever it was uploaded: any real photo (JPG, PNG, WebP, iPhone HEIC) comes
    back as an upright JPG no bigger than `max_side`. Always redrawn, so the location a phone hides
    in a photo never ends up on a public page. Raises ValueError on anything that isn't a photo."""
    from PIL import ImageOps
    try:
        probe = Image.open(io.BytesIO(contents))
        probe.verify()
        if probe.size[0] * probe.size[1] > _MAX_PIXELS:
            raise ValueError("too large")
        if probe.format not in ("JPEG", "MPO", "PNG", "WEBP", "HEIF"):
            raise ValueError("not a photo")
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(contents)))
        image.thumbnail((max_side, max_side), Image.LANCZOS)
        if image.mode in ("RGBA", "LA", "P"):
            image = image.convert("RGBA")
            flat = Image.new("RGB", image.size, (255, 255, 255))
            flat.paste(image, mask=image.split()[-1])
            image = flat
        out = io.BytesIO()
        image.convert("RGB").save(out, format="JPEG", quality=90, optimize=True)
    except Exception:
        raise ValueError("Please choose a JPG, PNG or iPhone photo.")
    return out.getvalue()


def clean_logo_png(contents: bytes, max_side: int = 2000) -> bytes:
    """Brand Kit logos may be PNG (they almost always have a see-through background). Confirms the
    bytes really are a PNG, then RE-ENCODES it - keeping the transparency, dropping metadata or
    anything appended after the image - and shrinks a huge file to `max_side` px so it never
    bloats a printed PDF. Raises ValueError on anything that isn't a genuine PNG."""
    try:
        probe = Image.open(io.BytesIO(contents))
        probe.verify()
    except Exception:
        raise ValueError("File is not a valid PNG image.")
    if probe.format != "PNG":
        raise ValueError("File is not a valid PNG image.")
    image = Image.open(io.BytesIO(contents))
    image = image.convert("RGBA")
    if max(image.size) > max_side:
        image.thumbnail((max_side, max_side), Image.LANCZOS)
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()
