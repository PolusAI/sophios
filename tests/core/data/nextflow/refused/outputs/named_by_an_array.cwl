cwlVersion: v1.2
class: CommandLineTool
doc: Make each file named, and give the file named by the whole array.
baseCommand: touch
inputs:
  xs:
    type: string[]
    inputBinding: {position: 1}
outputs:
  o:
    type: File
    outputBinding: {glob: $(inputs.xs).txt}
