cwlVersion: v1.2
class: Workflow
inputs:
  message: string
outputs:
  copied:
    type: File
    outputSource: copy/copied
steps:
- id: say
  run: say.cwl
  in:
    message: message
  out: [said]
- id: copy
  run: copy.cwl
  in:
    text: say/said
  out: [copied]
