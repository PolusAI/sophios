from sophios.apis.python.api import Step, Workflow


def small_workflow() -> Workflow:
    # scatter on all inputs
    # step array_ind
    array_ind = Step(clt_path='../../cwl_adapters/array_indices.cwl')
    array_ind.input_array = ["hello world", "not", "what world?"]
    array_ind.input_indices = [0, 1]
    # step echo
    echo = Step(clt_path='../../cwl_adapters/echo.cwl')
    echo.message = array_ind.output_array
    # set up inputs for scattering
    scatter_inps = echo.inputs[0]
    # assign the scatter and scatterMethod fields
    echo.scatter = [scatter_inps]

    # arrange steps
    steps = [array_ind, echo]

    # create workflow
    filename = 'scatter_pyapi_py'  # .yml
    wkflw = Workflow(steps, filename)
    return wkflw


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
