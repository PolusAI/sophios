{cwlVersion: v1.2, class: CommandLineTool, baseCommand: cp, arguments: [{position: 2, valueFrom: copy.txt}], inputs:
 {source: {type: File, inputBinding: {position: 1}}}, outputs: {result: {type: File, outputBinding: {glob: copy.txt}}}}
