# An image no registry has.
cwlVersion: v1.2
class: CommandLineTool
requirements:
  DockerRequirement:
    dockerPull: docker.io/nope:1
baseCommand: 'true'
inputs: {}
outputs: {}
