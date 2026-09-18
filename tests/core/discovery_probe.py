"""Deliberately unhermetic probe used to prove the runtime poison fires.

The file does not match pytest's default ``test_*.py`` pattern.  The
hermeticity suite invokes it explicitly under the poison plugin.
"""
import sophios.plugins


def test_plugin_discovery_is_attempted() -> None:
    """Reach the operation the hermetic oracle must never call."""
    sophios.plugins.get_tools_cwl({})
