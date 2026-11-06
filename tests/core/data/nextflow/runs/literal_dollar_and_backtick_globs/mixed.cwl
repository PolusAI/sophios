{cwlVersion: v1.2, class: CommandLineTool, baseCommand: touch, inputs: {name: {type: string}}, arguments:
 ['`$(inputs.name).txt'], outputs: {result: {type: File, outputBinding: {glob: '`$(inputs.name).txt'}}}}
