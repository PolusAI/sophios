from sophios.apis.python.api import Step, Workflow


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
    scatter_wic = workflow()
    scatter_wic.run()  # .run() here inside main
