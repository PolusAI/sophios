# User Guide

Recall for a moment the vague instructions your PhD advisor hastily scribbled onto the chalkboard about how to do a calculation. Now imagine that those scribbles were actually executable! That's the goal! The goal is to allow high-level, domain-specific concepts to be directly specified in a user-friendly YAML format. Then the abstract scientific protocol is automatically translated into specific concrete steps, executed on a remote job cluster, and automated analysis is performed.

## Main Features / Design Overview

See [overview](overview.md)

## auto-discovery

Many software packages have a way of automatically discovering files which they can use. (examples: [pytest](https://docs.pytest.org/en/latest/explanation/goodpractices.html#conventions-for-python-test-discovery) [pylint](https://pylint.pycqa.org/en/latest/user_guide/usage/run.html))

By default, sophios will recursively search for tools / workflows within the directories (and subdirectories) listed in the config file's json tags `search_paths_cwl` and `search_paths_wic`. The paths listed can be absolute or relative. The default `config.json` is shown.

***`We strongly recommend placing all repositories of tools / workflows in the same parent directory.`***

(All your repos should be side-by-side in sibling directories, as shown.)

```
......
"search_paths_cwl": {
        "global": [
            "../workflow-inference-compiler/cwl_adapters",
            "../image-workflows/cwl_adapters",
            "../biobb_adapters/biobb_adapters",
            "../mm-workflows/cwl_adapters"
        ],
        "gpu": [
            "../mm-workflows/gpu"
        ]
    },
"search_paths_wic": {
        "global": [
            "./workflow-inference-compiler/docs/tutorials",
            "../image-workflows/workflows",
            "../mm-workflows/examples"
        ]
    }
.....
```

If you do not specify config file using the command line argument `--config`, it will be automatically created for you the first time you run sophios in `~/wic/global_config.json`. (Because of this, the first time you run sophios you should be in the root directory of any one of your repos.) Then you can manually edit this file with additional sources of tools / workflows.

To avoid dealing with relative file paths in YAML files, by default

***`all tools / workflow names are required to be unique!`***

See [namespaces](advanced.md#namespaces) for details.

## Edges

What do the edges in a workflow represent? In many workflow languages (e.g. Argo), the edges represent dependencies between entire steps. Note that there could be ***`multiple`*** files or directories implicitly passed between two steps, but these workflow languages only model that as a single edge.

CWL models edges differently. In CWL, edges represent dependencies between ***`individual`*** explicit inputs and outputs. This fine-grained approach has several benefits, first and foremost increased parallelism. CWL also allows individual inputs and outputs to be tagged with metadata such as `type:` and `format:` tags. This additional information is what makes edge inference possible!

## Edge Inference Algorithm

First of all, a reminder that we can only connect an input in the current step to an output that already exists from some previous step.

The edge inference algorithm is actually rather simple: For each input with a given type and format, it checks for outputs that have the same type and format from one of the previous steps. Since many workflows are linear-ish pipelines, the steps are checked in reverse order (and the outputs of each step are also checked in reverse order). If there is a unique match, then great! If there are multiple matches, it arbitrarily chooses the first (i.e. most recent) match. For technical reasons edge inference is far from unique, so ***`users should always check that edge inference actually produces the intended DAG`***.

## Explicit Edges

If for some reason edge inference fails, you can always explicitly specify the edges using `!& var` and `!* var` notation. Simply use `!& var` to create a reference to an output and then, in an input in any later step, use `!* var` to dereference the output and create an explicit edge between the output and the input. See [`examples/gromacs`](https://github.com/PolusAI/mm-workflows/blob/main/examples/gromacs) in `mm-workflows` repository for a concrete example. This notation is intended to be similar to yaml's [anchors and aliases](https://support.atlassian.com/bitbucket-cloud/docs/yaml-anchors/) notation (which you can still use!).

## Inline Inputs

Of course, if you supply an input value directly, then the algorithm doesn't need to do either inference or explicit edges. The `message` input is a great example of this.

```
steps:
- echo:
    in:
      message: !ii Hello World
```

Note that this is one key difference between sophios and CWL. In CWL, all inputs must be given in a separate file. In sophios, inputs can be given inline with !ii and after compilation they will be automatically extracted into the separate file.

(NOTE: raw CWL is still supported with the --allow_raw_cwl flag.)

## Python API (experimental)
In addition to YAML based language for building workflows Sophios also provides a Python API. The aspirational goal of this API is to be close to regular
usage of Python. This API leverages YAML based syntax by transforming the Python workflow internally into a regular Sophios YAML workflow. All the Python API examples discussed here can be found in directory [`examples/scripts`](https://github.com/PolusAI/workflow-inference-compiler/tree/master/examples/scripts) in the Sophios repository.

### basics
Let us take the most basic workflow *`hello world`*. This is how we write it in YAML syntax.

```
steps:
- echo:
    in:
      message: !ii Hello World
```

The Python API closely follows the YAML syntax. We create steps and from steps we create workflows. The API exposes the means to create Step and Workflow objects. The steps and workflows are just plain objects in Python which can be passed around, manipulated, composed and reused. We can write the above workflow as follows using Python API.

```
from sophios.api.pythonapi import Step, Workflow


def workflow() -> Workflow:
    # step echo
    echo = Step(clt_path='../../cwl_adapters/echo.cwl')
    echo.message = 'hello world'
    # arrange steps
    steps = [echo]

    # create workflow
    filename = 'helloworld_pyapi_py'
    wkflw = Workflow(steps, filename)
    return wkflw

# Do NOT .run() here

if __name__ == '__main__':
    helloworld = workflow()
    helloworld.run()  # .run() here inside main
```

Here `echo` is a step object created by specifying the path of cwl adapter (a basic cwl workflow) `echo.cwl`. The the input to `echo` is `message` the user can assign value directly (i.e, inline) or create another cwl object compatible with the type of `message`. It is to be noted that we didn't have to specify if `message` is an input type, the specified attributes of the step object gets mapped to the corresponding `input` or `output` of the cwl step if it exists.

A workflow object is created using a list of steps in **`correct order`** and a unique filename. As there is only one step in this example hence the workflow object is created with a list containing only one step `echo`.

### multistep
Here is an example of a multistep workflow in YAML syntax. It is to be noted that the output of step `touch` is inferred by the compiler as the input *`file`*  of step `append` and the output *`file`* of `append` is inferred as input for the step `cat`.

```
steps:
- id: touch
  in:
    filename: !ii empty.txt
- id: append
  in:
    str: !ii Hello
- id: cat
```
We can write the above workflow as follows using the Python API.

```
from sophios.api.pythonapi import Step, Workflow


def workflow() -> Workflow:
    # step echo
    touch = Step(clt_path='../../cwl_adapters/touch.cwl')
    touch.filename = 'empty.txt'
    append = Step(clt_path='../../cwl_adapters/append.cwl')
    append.file = touch.file
    append.str = 'Hello'
    cat = Step(clt_path='../../cwl_adapters/cat.cwl')
    cat.file = append.file
    # arrange steps
    steps = [touch,append,cat]

    # create workflow
    filename = 'multistep1_pyapi_py'
    wkflw = Workflow(steps, filename)
    return wkflw

# Do NOT .run() here

if __name__ == '__main__':
    multistep1 = workflow()
    multistep1.run()  # .run() here inside main
```

It is the same process of creating step objects and using the step objects to create a workflow object. The difference is there is no support for edge inference! All the outputs and inputs for each step must be specified by the user and the user is responsible to correctly match the edges of each step. It is important to note that the user must **know** the names of the output of a step to specify as input of another step.

In the Python API step objects and workflow objects are purely syntactic constructions at the user level. No semantic transformation are done at this level. All the transformations are done by the Sophios compiler on the (internal) generated YAML workflow.

### scattering
An important feature of Sophios and CWL is scattering over an input in any given step. The following workflow is a simple example of `scatter` and the non-default `scatterMethod`.

```
# Demonstrates scattering on a subset of inputs and a non default scattering method
steps:
- id: array_indices
  in:
    input_array: !ii ["hello world", "not", "what world?"]
    input_indices: !ii [0,2]
  out:
    - output_array: !& filt_message
- id: echo_3
  scatter: [message1,message2]
  scatterMethod: flat_crossproduct
  in:
    message1: !* filt_message
    message2: !* filt_message
    message3: !ii scalar
```
We can write the above workflow as follows using the Python API.

```
from sophios.api.pythonapi import Step, Workflow

def workflow() -> Workflow:
    # scatter on a subset of inputs
    # step array_indices
    array_ind = Step(clt_path='../../cwl_adapters/array_indices.cwl')
    array_ind.input_array = ["hello world", "not", "what world?"]
    array_ind.input_indices = [0, 2]
    # step echo_3
    echo_3 = Step(clt_path='../../cwl_adapters/echo_3.cwl')
    echo_3.message1 = array_ind.output_array
    echo_3.message2 = array_ind.output_array
    echo_3.message3 = 'scalar'
    # set up inputs for scattering
    msg1 = echo_3.inputs[0]
    msg2 = echo_3.inputs[1]
    # assign the scatter and scatterMethod fields
    echo_3.scatter = [msg1, msg2]
    echo_3.scatterMethod = 'flat_crossproduct'

    # arrange steps
    steps = [array_ind, echo_3]

    # create workflow
    filename = 'scatter_pyapi_py'  # .yml
    wkflw = Workflow(steps, filename)
    return wkflw


# Do NOT .run() here

if __name__ == '__main__':
    scatter_wic = workflow()
    scatter_wic.run()  # .run() here inside main

```
Here again we see all the inputs and outputs are explicitly specified by the user and explicit edges are constructed from one output to another. Any attribute which is not an *input* or an *output* of a step object is a **special** attribute. `scatter` is just a **special (and optional)** attribute on the `echo_3` step object, just like in YAML the user must specify which inputs be scattered before applying the step.

The scatter attribute needs the actual input objects of the step not *just* the names as a list, this is quite similar to `scatter` tag in the YAML syntax. Similarly here a non-default scatter method is specified on `echo_3` through the (optional) `scatterMethod` attribute on the step that needs to be scattered.

The user must make sure that scatter operation described in the code is valid i.e, the arity of input data is compatible with scattering and scattering method. If there is any mismatch or mistake in the Python code, the API wouldn't be able to point to the exact issue in the Python code. The user will get a Sophios compiler error in that scenario and it might not be straightforward to pinpoint the error in the Python source code. Again it is to be noted that the objects in the Python API are purely syntactic at the user level.

### conditional
The Python API also supports conditional workflows. It transparently exposes the syntax and semantics of `when` tag of CWL. Here is a simple example of using `when` in a workflow.

```
steps:
  toString:
    in:
      input: !ii 27
    out:
    - output: !& string_int
  echo:
    when: '$(inputs.message < "27")'
    in:
      message: !* string_int
```

We can write the above workflow as follows using the Python API.

```
from sophios.api.pythonapi import Step, Workflow


def workflow() -> Workflow:
    # conditional on input
    # step toString
    toString = Step(clt_path='../../cwl_adapters/toString.cwl')
    toString.input = 27
    # step echo
    echo = Step(clt_path='../../cwl_adapters/echo.cwl')
    echo.message = toString.output
    # add a when clause
    # alternate js syntax
    # echo.when = '$(inputs["message"] < 27)'
    echo.when = '$(inputs.message < "27")'
    # since the condition is not met the echo step is skipped!

    # arrange steps
    steps = [toString, echo]

    # create workflow
    filename = 'when_pyapi_py'  # .yml
    wkflw = Workflow(steps, filename)
    return wkflw


# Do NOT .run() here

if __name__ == '__main__':
    when_wic = workflow()
    when_wic.run()  # .run() here inside main
```
Similar to `scatter`, `when` is a **special (and optional)** attribute to any step object in the Python API.
The `when` attribute of a step object exposes the exact same js embedded syntax of `when` tag of the YAML/CWL syntax. One has to be careful about appropriate escaping in the string input of `when` in Python API. In the above case the comparison is between two strings so "" is around the literal 27 (i.e. value after `toString` step).
## Partial Failures

In running workflows at scale, sometimes it is the case that one of the workflow steps may crash due to a bug causing the entire workflow to crash. In this case can use `--partial_failure_enable` flag. For special cases when the exit status of a workflow step isn't 1, and a different error code is returned (for example 142), then the user can supply the error code to sophios as a success code to prevent workflow from crashing with `--partial_failure_success_codes 0 1 142`. By default partial failure flag will consider only 0 and 1 as success codes. An example line snippet of the error code being printed is shown below.
```
[1;30mWARNING[0m [33m[job compare_extract_protein_pdbbind__step__4__topology_check] exited with status: 139[0m
```

## Parallelization

In order to utilize scattering features in cwl, the user needs to provide the flag `--parallel`. Additionally cwltool has various issues regarding scattering features such as deadlocks and thus it is preferred to use toil in this case `--cwl_runner toil-cwl-runner`.
