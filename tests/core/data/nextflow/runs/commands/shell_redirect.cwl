cwlVersion: v1.2
class: CommandLineTool
doc: Print one word holding a semicolon, appended to redirected.txt by the shell.
requirements:
  ShellCommandRequirement: {}
baseCommand: printf
arguments:
- {position: 1, valueFrom: '%s'}
- {position: 2, valueFrom: payload; not a separate command}
- {position: 3, valueFrom: '>>', shellQuote: false}
- {position: 4, valueFrom: redirected.txt}
inputs: {}
outputs:
  result:
    type: File
    outputBinding: {glob: redirected.txt}
