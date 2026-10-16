# Configuration reference

What may `~/wic/global_config.json` contain? Four keys, listed here with their
shapes. [Configuration](configuration.md) explains what each is for.

## Where it is read from

| Source | Rule |
|---|---|
| `--config_file FILE` | read as given; must exist, never written |
| `--homedir DIR` | the default becomes `DIR/wic/global_config.json` |
| neither | `~/wic/global_config.json`; written when missing, together with `~/wic/cwl_adapters/` and `~/wic/examples/` |

Relative paths in the file are resolved from the working directory of the
command that reads it. The command line reads the file on every compile;
`Workflow.compile()` and `Workflow.run()` do not.

## Keys

### `search_paths_cwl`

A mapping of namespace names to lists of directories searched recursively
for `*.cwl`. The `global` namespace is the one a step uses unless its
`wic: steps:` entry says `namespace: <name>`. A namespace whose value is not
a list is `TypeError` when the file is read.

```json
"search_paths_cwl": {
    "global": ["/home/you/wic/cwl_adapters", "/home/you/project/tools"],
    "gpu": []
}
```

### `search_paths_wic`

The same shape, for the `.wic` files a step may call and the real-time
analyses it may declare. Every `.wic` found is indexed by stem and read only
when a workflow calls it; a path containing `_inputs` is skipped.

```json
"search_paths_wic": {
    "global": ["/home/you/wic/examples", "/home/you/project/workflows"]
}
```

### `renaming_conventions`

A list of `[before, after]` pairs, applied in order to an input's name
(after `input_` is removed) before `--inference_use_naming_conventions`
compares it with an output's name (after `output_` is removed).

```json
"renaming_conventions": [
    ["energy_", "edr_"],
    ["structure_", "tpr_"],
    ["traj_", "trr_"]
]
```

### `inference_rules`

A mapping of format identifiers to `break` or `continue`. It applies to the
converter steps that `--insert_steps_automatically` adds: `break` stops
inference's backwards scan at an inserted step whose output carries the
format; `continue` is the default. A step you wrote sets its own rules with
`inference:` under its `wic: steps:` entry
([Edge inference](edge-inference.md)).

```json
"inference_rules": {
    "edam:format_3881": "continue",
    "edam:format_3987": "continue",
    "edam:format_3878": "break",
    "edam:format_2033": "break"
}
```

## The file Sophios writes

```json
{
    "inference_rules": {
        "edam:format_2033": "break",
        "edam:format_3878": "break",
        "edam:format_3881": "continue",
        "edam:format_3987": "continue"
    },
    "renaming_conventions": [
        ["energy_", "edr_"],
        ["structure_", "tpr_"],
        ["traj_", "trr_"]
    ],
    "search_paths_cwl": {
        "global": ["/home/you/wic/cwl_adapters"],
        "gpu": []
    },
    "search_paths_wic": {
        "global": ["/home/you/wic/examples"]
    }
}
```

Keys are written in sorted order. The two search paths are absolute, under
the home directory the command was given.
