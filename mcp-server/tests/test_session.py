

def test_default_project_name_is_importable_from_session():
    """`make_agent_project` imports this from the package, not the submodule.

    It lives in session/facade.py; without a re-export the import fails and
    step 2 of SETUP.md dies with an ImportError on a fresh install.
    """
    from fl_mcp.session import DEFAULT_PROJECT_NAME

    assert DEFAULT_PROJECT_NAME.endswith(".flp")


def test_make_agent_project_module_imports():
    """The whole module, as `python -m fl_mcp.make_agent_project` would."""
    import importlib

    assert importlib.import_module("fl_mcp.make_agent_project") is not None
