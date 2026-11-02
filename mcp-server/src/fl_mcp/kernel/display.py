"""Images a cell shows, like a notebook's inline output: the model sees what its code drew.

A cell shows an image by ending with it (a PIL image, a matplotlib figure, anything with
``_repr_png_``), by passing it to ``display()``, or by leaving matplotlib figures open
(``plt.show()`` included), which show when the cell ends. Each is downscaled to fit
MAX_PIXELS and encoded as PNG, or JPEG when that is much smaller (a photo, a dense
spectrogram).

The kernel runs model code, so the server checks every image it receives against the
same caps (ShownImage's validator): the header's format and size, and the byte count.
Audio is never shown: a model cannot take it, and it is millions of tokens as text.
"""

from __future__ import annotations

import io
import math
import struct
import sys
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Images one run can show; more are dropped and the result says so.
MAX_IMAGES = 4
# Claude reads an image as about width x height / 750 tokens and shrinks anything larger
# than about 1.15 megapixels anyway, so this is the most one image costs: ~1,500 tokens.
MAX_PIXELS = 1_150_000
MAX_EDGE = 1568
MAX_BYTES = 2 << 20
_TOKEN_PIXELS = 750
# A PNG this large is likely a photo, where JPEG at this quality is far smaller.
_JPEG_TRY_BYTES = 300_000
_JPEG_QUALITY = 85
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
_FILE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")


class ShownImage(BaseModel):
    """One image a cell showed, as it crosses the kernel wire."""

    model_config = ConfigDict(ser_json_bytes="base64", val_json_bytes="base64")

    format: Literal["png", "jpeg"]
    width: int = Field(ge=1, le=MAX_EDGE)
    height: int = Field(ge=1, le=MAX_EDGE)
    data: bytes = Field(max_length=MAX_BYTES)

    @model_validator(mode="after")
    def _is_the_image_it_claims(self) -> ShownImage:
        if self.width * self.height > MAX_PIXELS:
            raise ValueError(f"image is {self.width}x{self.height}, over {MAX_PIXELS} pixels")
        if image_size(self.format, self.data) != (self.width, self.height):
            raise ValueError("image header does not match its declared format and size")
        # The header bounds the pixels, so decoding is cheap; an image the client cannot
        # decode would fail every later request that carries this result.
        from PIL import Image

        try:
            with Image.open(io.BytesIO(self.data)) as decoded:
                decoded.load()
        except Exception as exc:
            raise ValueError(f"image does not decode: {exc}") from exc
        return self

    @property
    def mime_type(self) -> str:
        return f"image/{self.format}"

    def note(self, number: int) -> str:
        """What the result says about the image, so a client without images still knows."""
        tokens = math.ceil(self.width * self.height / _TOKEN_PIXELS)
        return f"image {number}: {self.width}x{self.height} {self.format.upper()}, about {tokens} tokens"


def describe(images: list[ShownImage], dropped: int) -> list[str]:
    """A result's ``images``: one line per image, and how many a run's cap dropped."""
    notes = [image.note(number) for number, image in enumerate(images, 1)]
    if dropped:
        notes.append(f"{dropped} more not shown: a run shows at most {MAX_IMAGES} images")
    return notes


def image_size(fmt: str, data: bytes) -> tuple[int, int] | None:
    """(width, height) from a PNG or JPEG header, without decoding the picture."""
    if fmt == "png":
        if data[:8] != _PNG_MAGIC or data[12:16] != b"IHDR" or len(data) < 24:
            return None
        return struct.unpack(">II", data[16:24])
    if fmt == "jpeg" and data[:3] == _JPEG_MAGIC:
        index = 2
        while index + 9 <= len(data):
            if data[index] != 0xFF:
                return None
            marker = data[index + 1]
            length = struct.unpack(">H", data[index + 2 : index + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):  # a start-of-frame
                height, width = struct.unpack(">HH", data[index + 5 : index + 9])
                return width, height
            index += 2 + length
    return None


class Display:
    """The images one cell has shown so far, in order, capped at MAX_IMAGES."""

    def __init__(self) -> None:
        self.images: list[ShownImage] = []
        self.dropped = 0
        # The objects themselves, not their ids: a freed temporary's id is reused by the next object.
        self._shown: list[Any] = []

    def show(self, *objects: Any) -> None:
        """display(): show each object; raises TypeError for one that is not an image."""
        for obj in objects:
            image = to_image(obj)
            if image is None:
                raise TypeError(
                    f"display() shows images: a PIL image, a matplotlib figure, PNG/JPEG bytes "
                    f"or an image file path, not {type(obj).__name__}"
                )
            self._add(obj, image)

    def show_result(self, value: Any) -> bool:
        """A cell's trailing expression: shown when it is an image, else repr'd by the caller."""
        if self._was_shown(value):
            return True  # already shown in this cell (display(img) then img)
        if isinstance(value, str | bytes | bytearray | Path):
            return False  # a path or raw bytes is shown only when asked, by display()
        image = to_image(value)
        if image is None:
            return False
        self._add(value, image)
        return True

    def flush_figures(self) -> None:
        """Show and close the matplotlib figures still open, like a notebook at a cell's end."""
        pyplot = sys.modules.get("matplotlib.pyplot")
        if pyplot is None:
            return
        for number in pyplot.get_fignums():
            figure = pyplot.figure(number)
            try:
                if not self._was_shown(figure):
                    self._add(figure, to_image(figure))
            finally:  # a figure that cannot draw must not fail every later cell too
                pyplot.close(figure)

    def _was_shown(self, obj: Any) -> bool:
        return any(shown is obj for shown in self._shown)

    def _add(self, obj: Any, image: Any) -> None:
        self._shown.append(obj)
        if len(self.images) >= MAX_IMAGES:
            self.dropped += 1
        else:
            self.images.append(encode(image))


# The display of the cell running now; the kernel's matplotlib backend shows figures into it.
current: Display | None = None


def to_image(obj: Any) -> Any:
    """A PIL image for anything showable, else None."""
    from PIL import Image

    if isinstance(obj, Image.Image):
        return obj
    if hasattr(obj, "savefig"):  # a matplotlib figure
        buffer = io.BytesIO()
        obj.savefig(buffer, format="png")
        return Image.open(io.BytesIO(buffer.getvalue()))
    if isinstance(obj, str | Path):
        path = Path(obj)
        return Image.open(path) if path.suffix.lower() in _FILE_SUFFIXES and path.is_file() else None
    data = bytes(obj) if isinstance(obj, bytes | bytearray) else _repr_image(obj)
    if data is not None and (data.startswith(_PNG_MAGIC) or data.startswith(_JPEG_MAGIC)):
        return Image.open(io.BytesIO(data))
    return None


def _repr_image(obj: Any) -> bytes | None:
    """The notebook display protocol: an object that can draw itself as PNG or JPEG."""
    for method in ("_repr_png_", "_repr_jpeg_"):
        render = getattr(obj, method, None)
        data = render() if callable(render) else None
        if isinstance(data, bytes):
            return data
    return None


def encode(image: Any) -> ShownImage:
    """Downscale to the caps and encode: PNG, or JPEG when that is much smaller."""
    from PIL import Image

    image.load()
    width, height = image.size
    scale = min(1.0, MAX_EDGE / max(width, height), (MAX_PIXELS / (width * height)) ** 0.5)
    if scale < 1.0:
        size = (max(1, int(width * scale)), max(1, int(height * scale)))
        image = image.resize(size, Image.Resampling.LANCZOS)
    if image.mode not in ("RGB", "RGBA", "L", "LA"):
        image = image.convert("RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB")
    fmt: Literal["png", "jpeg"] = "png"
    data = _encoded(image, "PNG")
    if len(data) > _JPEG_TRY_BYTES:
        jpeg = _encoded(image.convert("RGB"), "JPEG")
        if len(jpeg) < len(data) // 2 or len(data) > MAX_BYTES:
            fmt, data = "jpeg", jpeg
    return ShownImage(format=fmt, width=image.size[0], height=image.size[1], data=data)


def _encoded(image: Any, fmt: str) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **({"optimize": True} if fmt == "PNG" else {"quality": _JPEG_QUALITY}))
    return buffer.getvalue()
