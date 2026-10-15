cwlVersion: v1.2
class: CommandLineTool
baseCommand: [wc, -w]
requirements:
  DockerRequirement:
    dockerPull: debian:bookworm-slim
inputs:
  text:
    type: File
    inputBinding:
      position: 1
outputs:
  counts:
    type: File
    outputBinding:
      glob: counts.txt
stdout: counts.txt
