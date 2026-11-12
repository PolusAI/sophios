cwlVersion: v1.2
class: CommandLineTool
doc: Sleep `seconds`, then print first-seconds to out.txt; `skipped` is read only by the condition of the step.
baseCommand: [sh, -c, 'sleep "$2"; printf %s-%s "$1" "$2"', pair]
inputs:
  first:
    type: string
    inputBinding: {position: 1}
  seconds:
    type: string
    inputBinding: {position: 2}
  skipped: string
stdout: out.txt
outputs:
  line:
    type: File
    outputBinding: {glob: out.txt}
