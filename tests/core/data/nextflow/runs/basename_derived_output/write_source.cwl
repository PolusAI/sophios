{cwlVersion: v1.2, class: CommandLineTool, baseCommand: echo, inputs: {message: {type: string, inputBinding:
 {position: 1}}}, stdout: lines.txt, outputs: {result: {type: File, outputBinding: {glob: lines.txt}}}}
