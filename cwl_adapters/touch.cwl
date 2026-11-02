cwlVersion: v1.0

class: CommandLineTool

requirements:
  DockerRequirement:
    dockerPull: docker.io/library/debian:bookworm-slim
  InlineJavascriptRequirement: {}

baseCommand: touch

inputs:
  filename:
    type: string
    inputBinding:
      position: 1

outputs:
  file:
    type: File
    outputBinding:
      glob: $(inputs.filename)

