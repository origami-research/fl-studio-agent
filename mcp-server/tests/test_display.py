"""run shows images like a notebook, within caps the server enforces.

The model cannot hear a render but can look at a picture of it, and at any plot its own
code draws. These tests pin how a cell shows an image, the caps that bound what one run
costs in tokens, and that the server takes no image from the kernel on its word.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import types

import numpy as np
import pytest
from PIL import Image
from pydantic import ValidationError

from fl_mcp.kernel import display, wire
from fl_mcp.kernel.cell import new_namespace, run_cell


@pytest.fixture
def namespace():
    return new_namespace(None)


def test_a_cell_ending_with_an_image_shows_it_instead_of_its_repr(namespace):
    result = run_cell("from PIL import Image\nImage.new('RGB', (200, 100), 'red')", namespace)

    assert result.outcome == "ok"
    assert result.result is None
    assert result.images == ["image 1: 200x100 PNG, about 27 tokens"]
    [shown] = result.shown
    assert Image.open(io.BytesIO(shown.data)).size == (200, 100)


def test_display_shows_several_and_a_run_is_capped(namespace):
    code = "from PIL import Image\nfor i in range(6):\n    display(Image.new('L', (10 + i, 10)))"
    result = run_cell(code, namespace)

    assert [image.width for image in result.shown] == [10, 11, 12, 13]
    assert result.images[-1] == "2 more not shown: a run shows at most 4 images"


def test_a_large_image_is_downscaled_to_the_token_cap(namespace):
    result = run_cell("from PIL import Image\nImage.new('RGB', (4000, 3000))", namespace)

    [shown] = result.shown
    assert max(shown.width, shown.height) <= display.MAX_EDGE
    assert shown.width * shown.height <= display.MAX_PIXELS
    assert shown.width / shown.height == pytest.approx(4 / 3, abs=0.01)


def test_a_photo_like_image_is_sent_as_jpeg(namespace):
    noise = (np.random.default_rng(1).random((900, 900, 3)) * 255).astype("uint8")
    namespace["noise"] = noise
    result = run_cell("from PIL import Image\nImage.fromarray(noise)", namespace)

    [shown] = result.shown
    assert shown.format == "jpeg"
    assert len(shown.data) <= display.MAX_BYTES


def test_display_refuses_what_is_not_an_image(namespace):
    result = run_cell("display(42)", namespace)

    assert result.outcome == "error"
    assert "display() shows images" in result.traceback


def test_images_shown_before_an_error_still_show(namespace):
    result = run_cell("from PIL import Image\ndisplay(Image.new('RGB', (8, 8)))\n1 / 0", namespace)

    assert result.outcome == "error"
    assert len(result.shown) == 1


def test_a_path_or_bytes_ending_a_cell_is_repr_d_and_shows_only_through_display(namespace, tmp_path):
    path = tmp_path / "plot.png"
    Image.new("RGB", (30, 20)).save(path)
    namespace["path"] = str(path)

    assert run_cell("path", namespace).shown == []
    assert [image.width for image in run_cell("display(path)", namespace).shown] == [30]


def test_open_matplotlib_figures_show_once_at_the_end_of_the_cell(namespace, monkeypatch):
    """Like a notebook's inline backend: figures left open show when the cell ends, are
    then closed, and a figure the cell also ended with shows once."""

    class Figure:
        def savefig(self, buffer, format):
            Image.new("RGB", (64, 48)).save(buffer, format="PNG")

    figures = _fake_pyplot(monkeypatch, Figure(), Figure())
    namespace["first"] = figures[1]

    result = run_cell("first", namespace)

    assert len(result.shown) == 2
    assert figures == {}


def test_a_figure_that_cannot_draw_fails_its_cell_only(namespace, monkeypatch):
    class Broken:
        def savefig(self, buffer, format):
            raise ValueError("bad mathtext")

    figures = _fake_pyplot(monkeypatch, Broken())

    assert run_cell("1", namespace).outcome == "error"
    assert figures == {}  # closed anyway
    assert run_cell("x = 1", namespace).outcome == "ok"


def _fake_pyplot(monkeypatch, *open_figures):
    """matplotlib.pyplot's figure registry, as much as flush_figures uses."""
    figures = dict(enumerate(open_figures, 1))
    pyplot = types.ModuleType("matplotlib.pyplot")
    pyplot.get_fignums = lambda: sorted(figures)
    pyplot.figure = figures.__getitem__
    pyplot.close = lambda figure: [figures.pop(n) for n, f in list(figures.items()) if f is figure]
    monkeypatch.setitem(sys.modules, "matplotlib.pyplot", pyplot)
    return figures


def test_real_matplotlib_shows_through_the_backend_the_kernel_sets():
    """In a fresh interpreter with the kernel's MPLBACKEND, as child.py starts it."""
    pytest.importorskip("matplotlib")
    probe = (
        "from fl_mcp.kernel.cell import new_namespace, run_cell\n"
        "r = run_cell('import matplotlib.pyplot as plt\\nplt.plot([1, 3, 2])\\nplt.show()\\nplt.plot([2])', "
        "new_namespace(None))\n"
        "print(r.outcome, [(i.width, i.height) for i in r.shown])"
    )
    env = {**os.environ, "MPLBACKEND": "module://fl_mcp.kernel.mpl_inline"}
    out = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True, check=True)

    assert out.stdout.strip() == "ok [(640, 480), (640, 480)]", out.stderr


def _done_with(image: display.ShownImage) -> bytes:
    from fl_mcp.contract import RunResult

    return wire.encode(wire.Done.of(RunResult(outcome="ok", stdout="", actions=[]).with_images([image], 0)))


def test_the_server_refuses_an_image_whose_header_disagrees_with_its_claim():
    """The kernel runs model code: an image is accepted only when its own header has the
    format and size it claims, within the caps, so the token cost stated is the real one."""
    image = display.encode(Image.new("RGB", (100, 50)))
    assert wire.parse_from_kernel(_done_with(image)).run_result().shown[0].width == 100

    lying = _done_with(image).replace(b'"width":100', b'"width":10')
    with pytest.raises(ValidationError):
        wire.parse_from_kernel(lying)
    with pytest.raises(ValidationError):
        display.ShownImage(format="png", width=1500, height=1500, data=image.data)  # over the pixel cap
    with pytest.raises(ValidationError):  # a true header over bytes that are not that image
        display.ShownImage(format="png", width=100, height=50, data=image.data[:33] + b"\x00" * 1000)


def test_the_server_names_the_images_it_checked_not_what_the_kernel_says():
    image = display.encode(Image.new("RGB", (100, 50)))
    line = _done_with(image).replace(b"image 1: 100x50 PNG, about 7 tokens", b"no images at all")

    assert wire.parse_from_kernel(line).run_result().images == ["image 1: 100x50 PNG, about 7 tokens"]


def test_an_image_shown_then_freed_does_not_hide_the_next_one(namespace):
    """Shown objects are kept, not their ids: CPython reuses a freed temporary's address."""
    code = "from PIL import Image\ndisplay(Image.new('RGB', (8, 8)))\nImage.new('RGB', (9, 9))"

    assert [image.width for image in run_cell(code, namespace).shown] == [8, 9]


def test_image_size_reads_png_and_jpeg_headers():
    for fmt in ("PNG", "JPEG"):
        buffer = io.BytesIO()
        Image.new("RGB", (123, 45)).save(buffer, format=fmt)
        assert display.image_size(fmt.lower(), buffer.getvalue()) == (123, 45)
    assert display.image_size("png", b"not an image") is None
