cwlVersion: v1.2
class: Workflow
inputs:
  message: string
outputs:
  copied:
    type: File
    outputSource: copy/copied
steps:
  say:
    run: say.cwl
    in:
      message: message
    out: [said]
  copy:
    run: copy.cwl
    in:
      text: say/said
    out: [copied]
