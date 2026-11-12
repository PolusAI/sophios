cwlVersion: v1.2
class: CommandLineTool
doc: A containerized command that can render empty, so CWL would run the image's CMD.
requirements:
  DockerRequirement: {dockerPull: 'ubuntu:24.04'}
inputs:
  name: {type: 'string?', inputBinding: {prefix: --name}}
  verbose: {type: boolean, inputBinding: {prefix: -v}}
outputs: {}
