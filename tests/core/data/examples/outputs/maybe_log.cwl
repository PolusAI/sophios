cwlVersion: v1.2
class: CommandLineTool
doc: Make each file named; give the one .log file if there is one.
baseCommand: touch
inputs:
  names:
    type: string[]
    inputBinding: {position: 1}
outputs:
  o:
    type: File?
    outputBinding: {glob: '*.log'}
