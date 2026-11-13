cwlVersion: v1.2
class: CommandLineTool
doc: Write the word fanout, with no newline, to data.txt, asking for 2 cores and 64 MiB.
requirements:
  ResourceRequirement:
    coresMin: 2
    ramMin: 64
baseCommand: [printf, '%s', fanout]
inputs: {}
stdout: data.txt
outputs:
  result:
    type: File
    outputBinding: {glob: data.txt}
