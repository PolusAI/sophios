class: CommandLineTool
cwlVersion: v1.0

requirements:
  DockerRequirement:
    dockerPull: docker.io/library/debian:bookworm-slim
  InlineJavascriptRequirement: {}

baseCommand: [cat]

inputs:
  file:
    type: File

outputs:
  output:
    type: string
    outputBinding:
      glob: output
      loadContents: true
      outputEval: $(self[0].contents)

stdin: $(inputs.file.path)
stdout: output