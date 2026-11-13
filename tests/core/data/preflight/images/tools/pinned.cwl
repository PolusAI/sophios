# Pulled, so nothing is loaded or imported for it.
cwlVersion: v1.2
class: CommandLineTool
requirements:
  DockerRequirement:
    dockerPull: docker.io/bash:4.4
    dockerImageId: bash:4.4
baseCommand: 'true'
inputs: {}
outputs: {}
