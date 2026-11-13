# An image loaded from the archive beside the run.
cwlVersion: v1.2
class: CommandLineTool
requirements:
  DockerRequirement:
    dockerLoad: image.tar
baseCommand: 'true'
inputs: {}
outputs: {}
