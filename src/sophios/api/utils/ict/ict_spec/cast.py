import json
from pathlib import Path

from yaml import safe_load

from sophios.api.utils.ict.ict_spec.model import ICT


def cast_to_ict(ict: Path | str | dict) -> ICT:
    """Build an ICT model from a path to a yaml/json file, or from a raw dict.

    Any ``ui`` entry is dropped before construction, since ICT here only
    models the tool spec used to generate a CommandLineTool, not its UI.

    Args:
        ict (Path | str | dict): Path (or path string) to a ``.yaml``/``.yml``/``.json``
            ICT file, or a dict already containing the ICT fields.

    Returns:
        ICT: The parsed ICT model.
    """
    if isinstance(ict, str):
        ict = Path(ict)

    if isinstance(ict, Path):

        if str(ict).endswith(".yaml") or str(ict).endswith(".yml"):
            with open(ict, "r", encoding="utf-8") as f_o:
                data = safe_load(f_o)
        elif str(ict).endswith(".json"):
            with open(ict, "r", encoding="utf-8") as f_o:
                data = json.load(f_o)
        else:
            raise ValueError(f"File extension not supported: {ict}")

        data.pop("ui", None)

        return ICT(**data)

    ict.pop("ui", None)

    return ICT(**ict)
