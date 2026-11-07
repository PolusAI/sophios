{cwlVersion: v1.2, class: CommandLineTool, baseCommand: echo, inputs: {message: {type: string, inputBinding:
 {position: 1}}}, stdout: message.txt, outputs: {result: {type: File, outputBinding: {glob: message.txt}}}}
