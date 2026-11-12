cwlVersion: v1.2
class: CommandLineTool
doc: A variable with a field nothing lowers.
requirements:
  EnvVarRequirement:
    envDef: {A: {envValue: x, foo: 1}}
baseCommand: env
inputs: {}
outputs: {}
