# An image imported under its id.
cwlVersion: v1.2
class: CommandLineTool
requirements:
  DockerRequirement:
    dockerImport: https://example.org/rootfs.tar
    dockerImageId: rootfs:1
baseCommand: 'true'
inputs: {}
outputs: {}
