cwlVersion: v1.2
class: CommandLineTool
doc: >-
  The shape of an image-workflows plugin: it is given the directory to write into, stages it writable, writes
  result.txt there and gives the directory back.
requirements:
  InlineJavascriptRequirement: {}
  InitialWorkDirRequirement:
    listing:
    - entry: $(inputs.outDir)
      writable: true
baseCommand: [sh, -c, 'printf written > "$2/result.txt"', plugin]
inputs:
  outDir:
    type: Directory
    inputBinding: {position: 1, prefix: --outDir}
outputs:
  outDir:
    type: Directory
    outputBinding: {glob: $(inputs.outDir.basename)}
