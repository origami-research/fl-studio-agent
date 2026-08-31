"""The ONLY OS/FL-coupled code in fl_mcp, quarantined (docs/29 §3).

Three concerns, one module each: where FL and our data live on this machine
(``paths``), whether FL is running and how to stop it (``process``), and how
our scripts get installed into FL (``install``). Everything else in fl_mcp
stays portable and testable without an FL Studio installation.
"""
