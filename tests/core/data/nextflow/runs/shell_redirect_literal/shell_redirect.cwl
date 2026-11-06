{cwlVersion: v1.2, class: CommandLineTool, requirements: {ShellCommandRequirement: {}}, baseCommand: printf, inputs: {},
 arguments: [{position: 1, valueFrom: '%s'}, {position: 2, valueFrom: payload; not a separate command}, {position: 3,
 valueFrom: '>>', shellQuote: false}, {position: 4, valueFrom: redirected.txt}], outputs: {result: {type: File,
 outputBinding: {glob: redirected.txt}}}}
