{cwlVersion: v1.2, class: CommandLineTool, baseCommand: [touch, unmatched.txt, a.log, b.log], inputs: {},
    outputs: {o: {type: 'File?',
 outputBinding: {glob: '*.log'}}}}
