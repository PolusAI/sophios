{cwlVersion: v1.2, class: CommandLineTool, requirements: {ShellCommandRequirement: {}}, baseCommand: echo, inputs: {},
 arguments: [{position: 1, valueFrom: first}, {position: 2, valueFrom: ;, shellQuote: false}, {position: 3, valueFrom:
 echo}, {position: 4, valueFrom: second}], stdout: out.txt, outputs: {result: {type: File, outputBinding: {glob:
 out.txt}}}}
