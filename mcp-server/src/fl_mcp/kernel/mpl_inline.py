"""matplotlib's backend inside the kernel: ``plt.show()`` shows figures in the run result.

The kernel sets ``MPLBACKEND`` to this module, so a cell's figures never open a window
on the machine running FL; they become images the model sees (display.py), as in a
notebook. matplotlib is an engine dependency, so it imports without a %pip install;
this loads only when model code imports it.
"""

from __future__ import annotations

from typing import Any

from matplotlib.backend_bases import FigureManagerBase
from matplotlib.backends.backend_agg import FigureCanvasAgg

from fl_mcp.kernel import display

FigureCanvas = FigureCanvasAgg
FigureManager = FigureManagerBase


def show(*args: Any, **kwargs: Any) -> None:
    if display.current is not None:
        display.current.flush_figures()
