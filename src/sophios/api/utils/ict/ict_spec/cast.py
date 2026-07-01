import json
from pathlib import Path
from typing import Union

from yaml import safe_load

from sophios.api.utils.ict.ict_spec.model import ICT


def cast_to_ict(ict: Union[Path, str, dict]) -> ICT:

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
