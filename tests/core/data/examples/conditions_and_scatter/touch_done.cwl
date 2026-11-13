cwlVersion: v1.2
class: CommandLineTool
doc: Make done.txt, whatever `maybe` is, null included.
baseCommand: [touch, done.txt]
inputs:
  maybe: string?
outputs:
  done:
    type: File
    outputBinding: {glob: done.txt}
