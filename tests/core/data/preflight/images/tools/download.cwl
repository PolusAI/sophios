# An image downloaded, then loaded.
cwlVersion: v1.2
class: CommandLineTool
requirements:
  DockerRequirement:
    dockerLoad: https://example.org/image.tar
baseCommand: 'true'
inputs: {}
outputs: {}
