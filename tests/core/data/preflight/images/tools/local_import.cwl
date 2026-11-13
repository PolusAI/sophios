# An image imported from the archive beside the run.
cwlVersion: v1.2
class: CommandLineTool
requirements:
  DockerRequirement:
    dockerImport: rootfs.tar
    dockerImageId: rootfs:1
baseCommand: 'true'
inputs: {}
outputs: {}
