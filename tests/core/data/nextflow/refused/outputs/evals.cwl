cwlVersion: v1.2
class: CommandLineTool
doc: Every output states an outputEval that is not one of the two the Nextflow target reads as a declaration.
baseCommand: echo
inputs:
  message:
    type: string
    inputBinding: {position: 1}
stdout: out.txt
outputs:
  # $(self[0]) with a space anywhere inside it is another text
  spaces_inside:
    type: File
    outputBinding: {glob: out.txt, outputEval: '$( self[0] )'}
  # an expression is not evaluated, composed or recognised in part
  an_input:
    type: File
    outputBinding: {glob: out.txt, outputEval: '$(inputs.message)'}
  a_function_body:
    type: File
    outputBinding: {glob: out.txt, outputEval: '${return self[0];}'}
  text_before_it:
    type: File
    outputBinding: {glob: out.txt, outputEval: 'prefix-$(self[0])'}
  # a field of the file that a Nextflow task cannot give
  the_path:
    type: File
    outputBinding: {glob: out.txt, outputEval: '$(self[0].path)'}
