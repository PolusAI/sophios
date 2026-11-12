cwlVersion: v1.2
class: CommandLineTool
doc: A shell command line in a container, an image to build, and network access withheld.
requirements:
  DockerRequirement: {dockerPull: alpine, dockerFile: FROM alpine}
  ShellCommandRequirement: {}
  NetworkAccess: {networkAccess: false}
baseCommand: 'true'
inputs: {}
outputs: {}
