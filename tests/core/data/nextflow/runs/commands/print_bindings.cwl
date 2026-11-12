cwlVersion: v1.2
class: CommandLineTool
doc: Print the variable GREETING, then each argument the bindings make, one per line, into bindings.txt.
requirements:
  EnvVarRequirement:
    envDef: {GREETING: hello  there}
baseCommand: [sh, -c, 'printf "%s\n" "$GREETING" "$@"', sh]
inputs:
  verbose: {type: boolean, inputBinding: {position: 1, prefix: -v}}
  quiet: {type: boolean, inputBinding: {position: 2, prefix: -q}}
  ratios: {type: 'float[]', inputBinding: {position: 3, prefix: --ratio}}
  tag: {type: 'string?', inputBinding: {position: 4, prefix: --tag}}
  label: {type: 'string?', inputBinding: {position: 5, prefix: --label=, separate: false}}
  unset: {type: 'string?', inputBinding: {position: 6, prefix: --unset}}
  scale: {type: float, inputBinding: {position: 7}}
stdout: bindings.txt
outputs:
  result:
    type: File
    outputBinding: {glob: bindings.txt}
