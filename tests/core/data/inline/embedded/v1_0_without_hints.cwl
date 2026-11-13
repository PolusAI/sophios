# No hints key is the usual case, not an exception; ports written as lists are listed alike.
cwlVersion: v1.0
class: CommandLineTool
baseCommand: 'true'
inputs:
- {id: name, type: File, secondaryFiles: .fai}
outputs:
- {id: file, type: File, secondaryFiles: .bai, outputBinding: {glob: f}}
