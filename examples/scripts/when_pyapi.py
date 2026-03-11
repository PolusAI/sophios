from sophios.apis.python.api import Step, Workflow


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
    # echo.when = '$(inputs["message"] < "27")'
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
