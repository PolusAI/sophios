# Install Guide

## download

You will first need to install the [git](https://git-scm.com) version control system. Then run the following commands:

```
git clone https://github.com/PolusAI/sophios.git
cd workflow-inference-compiler

cd install/
./install_conda.sh
source ~/.bashrc
conda create --name wic # or any name
conda activate wic
./install_system_deps.sh
cd ..

pip install -e ".[all]"
pre-commit install  # Required for developers

sophios --generate_schemas
```

Developers should fork the upstream repository and clone their fork using the git@ url, then run the command

```
git remote add polusai https://github.com/PolusAI/sophios.git
```

## docker

Most of the plugins have been packaged up into [docker](https://www.docker.com) containers. If docker is not installed, you can compile workflows and generate DAGs but the workflows will fail at runtime.

### known issues

When running Docker for Mac, in some cases execution will hang. See [all containers hang](https://github.com/docker/for-mac/issues/5081) and [error waiting for container](https://github.com/docker/for-mac/issues/5139) for details. The current workaround is to simply restart Docker.

If you are experiencing hanging, and if the command `ps aux | grep com.docker | wc -l` returns more than 1000, this is likely the issue. If restarting Docker via the GUI doesn't work, try `sudo pkill com.docker && sudo pkill Docker`.

## podman

Alternatively, instead of docker you can use [podman](https://podman.io/whatis.html). podman is a daemonless (more secure) way to run containers. On linux, you can install podman via conda `conda install -c conda-forge podman` or using distro-specific methods. To run workflows with podman, simply append `--container_engine podman` after `--run_local`.

## Installation on Windows

Although compiling workflows natively on Windows is supported, the underlying `cwltool` runner currently requires [Windows Subsystem for Linux](https://learn.microsoft.com/en-us/windows/wsl/install#install-wsl-command). To install WSL, simply open PowerShell or Windows Command Prompt in **administrator** mode and run the command `wsl --install`. For more information about WSL, see the official [FAQ](https://learn.microsoft.com/en-us/windows/wsl/faq).

To install podman inside of WSL, simply run (from inside WSL) `sudo apt-get update && sudo apt-get install uidmap podman`.

### known issues

Network performance on WSL can be [very slow](https://github.com/microsoft/WSL/issues/4901). This appears to be caused by the "Large Send Offload Version 2" network setting. See [this article](https://townsyio.medium.com/wsl2-how-to-fix-download-speed-3edb0c348e29) for how to disable this setting.

## conda

[conda](https://en.wikipedia.org/wiki/Conda_(package_manager)) is an open source, cross platform package management system. It can install both Python dependencies and system binary dependencies, so conda is essentially a replacement for `pip`. The associated package distributions named `anaconda` and `miniconda` provide a database of packages that can be used with the `conda` command. (There is also an open source package distribution called [conda-forge](https://conda-forge.org)) Either one works, so if you already have anaconda installed then great. Otherwise, [miniconda](https://docs.conda.io/en/latest/miniconda.html) is all you need.

You can install conda and the system dependencies with the following commands:

```
./install_conda.sh
source ~/.bashrc
conda create --name wic
conda activate wic
./install_system_deps.sh
```

Note that if you close your terminal, the next time you open your terminal you will need to re-activate the wic environment:

```
conda activate wic
```

## install

To install into the wic environment, simply use the following command:

```
pip install -e ".[all]"
```

Developers should also install the git pre-commit hooks:
```
pre-commit install
```

You should now have the `wic` executable available in your terminal.

## testing

To test your installation, you can run the example in README.md:

```
sophios --yaml ../workflow-inference-compiler/docs/tutorials/helloworld.wic --run_local --quiet
```

You can also run the automated test suite. Note that the tests are based on the workflows; if you have more workflows, the tests will take longer.

(Also, if your workflows require CUDA, you will need an Nvidia GPU.)

```
pytest -m serial && pytest -m "not serial" --workers 8
```

If you're in a hurry, just run

```
pytest -m serial
```

which tests the compiler only (not the runtime) and only takes about a minute.

### known issues

At runtime, the following warning message may be printed to the console repeatedly:

```
WARNING: No ICDs were found. Either,
- Install a conda package providing a OpenCL implementation (pocl, oclgrind, intel-compute-runtime, beignet) or
- Make your system-wide implementation visible by installing ocl-icd-system conda package.
```

It does not appear to cause any problems, and moreover the suggested solutions don't seem to work, so it appears that this warning can be ignored.

## documentation

The latest documentation is available on [readthedocs](https://workflow-inference-compiler.readthedocs.io/en/latest/) as well as under the `docs/` folder. To build the documentation in html format, use the commands

```
cd docs/
make html
```

Then simply open the file `docs/_build/html/index.html` using any web browser.

## vscode extensions

When you open the project folder, VSCode should prompt you to install the following list of recommended extensions.

These vscode extensions are strongly recommended for users:

* YAML (Red Hat)

These vscode extensions are recommended for developers:

* Python
* MyPy
* Remote Development
* Docker
* CWL (Rabix/Benten)
* Git Extension Pack
* Github Actions (Mathieu Dutour)
* autoDocstring