cwlVersion: v1.2
class: CommandLineTool
doc: Exit codes besides 0, a union of a boolean and strings, and a requirement past what a task can withhold.
requirements:
  ToolTimeLimit: {timelimit: 60}
successCodes: [0, 1]
baseCommand: echo
inputs:
  flag: {type: ['null', boolean, {type: array, items: string}], inputBinding: {position: 1}}
outputs: {}
