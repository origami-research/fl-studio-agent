# Bundled plugin parameter models

Each `*.json` file is one `PluginParamModel`: a plugin name and its verified
parameter semantics. Add captures only with evidence for every declared kind,
range, unit, step, or option label. The loader validates every file at startup;
an invalid capture prevents the server from starting rather than silently
changing parameter interpretation.

There are no bundled models yet. The documented FL API exposes normalized
values and names but not the display semantics required to author a truthful
model. See `docs/research/plugin-param-model-sytrus.md` for the capture path.
