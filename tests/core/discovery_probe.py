"""Deliberately unhermetic probe proving the runtime poison fires; not a `test_*.py` name, it is invoked explicitly
under the poison plugin."""
import sophios.plugins


def test_plugin_discovery_is_attempted() -> None:
    """Reach the operation the hermetic oracle must never call."""
    sophios.plugins.get_tools_cwl({})
