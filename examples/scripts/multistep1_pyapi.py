from sophios.apis.python.api import Step, Workflow


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
    steps = [touch, append, cat]

    # create workflow
    filename = 'multistep1_pyapi_py'
    wkflw = Workflow(steps, filename)
    return wkflw

# Do NOT .run() here


if __name__ == '__main__':
    multistep1 = workflow()
    multistep1.run()  # .run() here inside main
