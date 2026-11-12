cwlVersion: v1.2
class: CommandLineTool
doc: Variables that are not literal text or one string input's value, read whole, and entries that are not entries.
requirements:
  EnvVarRequirement:
    envDef:
    - {envName: FROM_AN_INT, envValue: $(inputs.n)}
    - {envName: FROM_A_PATH, envValue: $(inputs.f.path)}
    - {envName: FROM_A_BASENAME, envValue: $(inputs.s.basename)}
    - {envName: BESIDE_TEXT, envValue: pre-$(inputs.s)}
    - {envName: JAVASCRIPT, envValue: '${return 1}'}
    - {envName: RUNTIME, envValue: $(runtime.cores)}
    - {envName: 1A, envValue: x}
    - {envName: NOT_TEXT, envValue: 3}
    - A=x
baseCommand: env
inputs:
  s: string
  n: int
  f: File
outputs: {}
