"""Third-party .flp test inputs, fetched on demand (never vendored).

These are real FL Studio project files used as *inputs* to our parse and
reconstruction integration tests. They are downloaded into ./data/ (gitignored)
at test time and used, not redistributed - so their upstream licenses (e.g.
GPL) do not attach to this repo. Each entry records its source and license.

To extend the corpus, add permissively- or clearly-licensed .flp URLs here.
"""

# name: (url, source, license)
CORPUS = {
    # PyFLP's own parser test fixtures - purpose-built, cover several shapes.
    "pyflp_fl_20_8_4.flp": (
        "https://raw.githubusercontent.com/demberto/PyFLP/master/tests/assets/FL%2020.8.4.flp",
        "github.com/demberto/PyFLP",
        "GPL-3.0 (used as test input, not redistributed)",
    ),
    "pyflp_multi_channel.flp": (
        "https://raw.githubusercontent.com/demberto/PyFLP/master/tests/assets/patterns/multi-channel.flp",
        "github.com/demberto/PyFLP",
        "GPL-3.0 (used as test input, not redistributed)",
    ),
}

# Deliberately-corrupt files, for parser robustness (must not crash our tools).
CORRUPT = {
    "corrupt_invalid_format.flp": (
        "https://raw.githubusercontent.com/demberto/PyFLP/master/tests/assets/corrupted/invalid-format.flp",
        "github.com/demberto/PyFLP",
        "GPL-3.0 (used as test input, not redistributed)",
    ),
    "corrupt_invalid_header_magic.flp": (
        "https://raw.githubusercontent.com/demberto/PyFLP/master/tests/assets/corrupted/invalid-header-magic.flp",
        "github.com/demberto/PyFLP",
        "GPL-3.0 (used as test input, not redistributed)",
    ),
}
