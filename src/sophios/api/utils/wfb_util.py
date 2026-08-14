from sophios.wic_types import PluginNodeConfig

_DIRECTORY_TYPES = {"directory", "file", "path", "collection", "csvCollection"}


def is_directory(input_dict: dict) -> bool:
    """Check if the given input dictionary represents a directory.

    Args:
        input_dict (dict): The input dictionary containing type and name.

    Returns:
        bool: True if the input represents a directory, False otherwise.
    """
    if input_dict.get("type", "") in _DIRECTORY_TYPES:
        return True

    name = input_dict.get("name", "").lower()
    is_dir: bool = name == "file" or name.endswith("path") or name.endswith("dir")
    return is_dir


def get_node_config(plugin: dict) -> PluginNodeConfig:
    """Get the UI configuration for a specific plugin.

    Args:
        plugin (dict): The plugin dictionary containing UI and inputs.

    Returns:
        PluginNodeConfig: A dictionary containing UI inputs, non-UI inputs, and outputs.
    """
    uis = plugin.get("ui", [])
    plugin_inputs = plugin.get("inputs", [])

    # split inputs into UI (form) and non-UI (circle inlets)
    non_ui_inputs = []  # circle inlets on the left side of the node
    ui_inputs = []  # UI inputs such as text fields, checkboxes, etc.

    for plugin_input in reversed(plugin_inputs):
        # find the UI element that corresponds to this input
        ui_input = next(
            (x for x in uis if "key" in x and x["key"] == "inputs." + plugin_input["name"]),
            None,
        )
        is_dir = is_directory(plugin_input)

        # if input is a directory - move it to the non-UI section
        if is_dir:
            non_ui_inputs.append(plugin_input)

        # in some cases UI is missing for the input, so we need to create it
        # but only if it's not a directory
        if not ui_input and not is_dir:
            calculated_ui_input = {
                "key": "inputs." + plugin_input["name"],
                "type": plugin_input["type"],
                "title": plugin_input["name"],
                "required": plugin_input["required"],
                "format": plugin_input["format"],
            }

            ui_inputs.append(calculated_ui_input)

        if ui_input and not is_dir:
            ui_input["required"] = plugin_input["required"]
            ui_input["format"] = plugin_input["format"]
            ui_inputs.append(ui_input)

    outputs = plugin.get("outputs", [])

    # if output has UI - move it to the UI section
    # this is mostly for internal nodes such as Input Data Directory
    for output in outputs:
        ui_output = next(
            (x for x in uis if "key" in x and x["key"] == "outputs." + output["name"]),
            None,
        )
        if ui_output:
            ui_inputs.append(ui_output)

    result: PluginNodeConfig = {"ui": ui_inputs, "inputs": non_ui_inputs, "outputs": outputs}
    return result
