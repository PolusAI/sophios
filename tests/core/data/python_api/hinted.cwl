# A tool whose image is a DockerRequirement hint, not a requirement.
cwlVersion: v1.2
class: CommandLineTool
baseCommand: 'true'
inputs: {}
outputs: {}
hints:
  DockerRequirement:
    dockerPull: docker.io/alpine:3.20
