cwlVersion: v1.2
class: CommandLineTool
doc: Make each file named; give maybe.txt if it is among them.
baseCommand: touch
inputs:
  names:
    type: string[]
    inputBinding: {position: 1}
outputs:
  o:
    type: File?
    outputBinding: {glob: maybe.txt}
