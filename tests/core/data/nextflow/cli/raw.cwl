cwlVersion: v1.2
class: Workflow
doc: A plain CWL workflow of the shipped echo tool.
inputs:
  msg: {type: string, default: hi}
outputs: {}
steps:
  say:
    run: ../../../../../cwl_adapters/echo.cwl
    in: {message: msg}
    out: [stdout]
