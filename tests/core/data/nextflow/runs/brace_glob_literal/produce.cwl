{cwlVersion: v1.2, class: CommandLineTool, baseCommand: [sh, -c, "echo literal > 'a{b,c}.txt'; echo decoy > ab.txt"],
 inputs: {}, outputs: {result: {type: File, outputBinding: {glob: 'a{b,c}.txt'}}}}
