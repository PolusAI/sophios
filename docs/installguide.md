# Install Guide

This page is for installing Sophios as a package. It focuses on two things:

- installing the Python package with `pip`,
- installing the non-Python tools that local workflow execution may need.

If you want to edit Sophios itself, run the test suite, build the documentation,
or use the repository examples directly from a checkout, use the
[Developer Install Guide](dev/installguide.md) instead.

## Python Requirement

Sophios declares its supported Python range in `pyproject.toml`. For the current
package, use Python 3.11 or newer.

Check your interpreter:

```bash
python --version
```

If your system Python is older, create an environment with a newer Python before
installing Sophios.

## Install Sophios

Using a virtual environment is recommended:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install sophios
```

On Windows PowerShell, activate the virtual environment with:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install sophios
```

Verify the command-line entry point:

```bash
sophios --help
```

Verify the public Python APIs:

```bash
python - <<'PY'
from sophios.api.python.workflow import Step, Workflow
from sophios.api.python.tool_builder import CommandLineTool, Input, Output, cwl
from sophios.compute_request import ComputeRequest

print("Sophios is installed")
PY
```

## What `pip install sophios` Provides

The `sophios` package installs the Python libraries needed for authoring,
compiling, validating, and running workflows through the supported Python APIs
and CLI.

That includes the Python packages for:

- CWL compilation and local runner integration,
- `.wic` YAML parsing and validation,
- Python workflow authoring with `Step` and `Workflow`,
- Python tool authoring with `CommandLineTool`,
- compute request construction and validation.

`pip` does not install every system executable that a workflow may call. The
next section covers those tools.

## Install Non-Python Runtime Tools

The exact tools you need depend on the workflows you run.

### Container Runtime

Install Docker or Podman when workflows use containerized CWL tools.

Sophios can author and compile workflows without Docker or Podman, but local
execution of containerized tools requires a working container runtime.

Check one of:

```bash
docker --version
podman --version
```

For Docker, Docker Desktop is the usual path on macOS and Windows. On Linux,
install Docker or Podman through your distribution package manager.

### Node.js

Install Node.js when workflows use CWL JavaScript expressions such as
`InlineJavascriptRequirement`.

Check:

```bash
node --version
```

Install examples:

```bash
conda install -c conda-forge nodejs
```

```bash
brew install node
```

```bash
sudo apt-get update
sudo apt-get install -y nodejs
```

### Graphviz

Install the Graphviz system package when you want rendered workflow diagrams.
The Python `graphviz` package installed by `pip` is only the Python binding; the
`dot` executable is a separate system dependency.

Check:

```bash
dot -V
```

Install examples:

```bash
conda install -c conda-forge graphviz
```

```bash
brew install graphviz
```

```bash
sudo apt-get update
sudo apt-get install -y graphviz
```

### Tools Invoked by Your Workflow

Sophios describes workflows. It does not install every command-line program
that your workflow might invoke.

For example, if your CWL tool runs `samtools`, `python`, `bash`, or a project
specific executable outside a container, that executable must be available in
the runtime environment.

## Quick Environment Check

This check reports the optional system tools Sophios commonly uses:

```bash
python - <<'PY'
import shutil

checks = {
    "node": "needed for CWL JavaScript expressions",
    "dot": "needed for Graphviz diagrams",
    "docker": "needed for Docker-backed local execution",
    "podman": "needed for Podman-backed local execution",
}

for executable, reason in checks.items():
    path = shutil.which(executable)
    status = path if path else "not found"
    print(f"{executable:>6}: {status} ({reason})")
PY
```

Missing optional tools are not always errors. Install the tools required by the
workflows you plan to run.

## Next Steps

After installation, start with the [Python Workflow API](userguide.md). If you
need standalone `.wic` workflows or YAML editor validation, see
[Advanced YAML and Operations](advanced.md).
