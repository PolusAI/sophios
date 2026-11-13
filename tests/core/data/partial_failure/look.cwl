# A real-time analysis, compiled under the workflow's options.
cwlVersion: v1.2
class: CommandLineTool
baseCommand: 'true'
inputs: {file: File}
outputs: {seen: {type: File, outputBinding: {glob: seen.txt}}}
