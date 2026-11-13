# An image pulled by name.
cwlVersion: v1.2
class: CommandLineTool
requirements:
  DockerRequirement:
    dockerPull: docker.io/bash:4.4
baseCommand: 'true'
inputs: {}
outputs: {}
