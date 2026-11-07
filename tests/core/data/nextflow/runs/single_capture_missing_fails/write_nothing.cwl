{cwlVersion: v1.2, class: CommandLineTool, baseCommand: 'true', inputs: {message: {type: string, inputBinding:
 {position: 1}}}, outputs: {result: {type: File, outputBinding: {glob: out.txt, outputEval: '$(self[0])'}}}}
