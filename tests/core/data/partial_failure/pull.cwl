# Runs in an image the runner pulls.
cwlVersion: v1.2
class: CommandLineTool
baseCommand: 'true'
requirements:
  DockerRequirement:
    dockerPull: docker.io/library/debian:bookworm-slim
inputs: {}
outputs: {}
