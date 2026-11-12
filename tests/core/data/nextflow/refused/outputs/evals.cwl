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
  space_in_the_index:
    type: File
    outputBinding: {glob: out.txt, outputEval: '$(self[ 0 ])'}
  space_before_the_end:
    type: File
    outputBinding: {glob: out.txt, outputEval: '$(self[0] )'}
  space_before_the_index:
    type: File
    outputBinding: {glob: out.txt, outputEval: '$(self [0])'}
  space_before_contents:
    type: File
    outputBinding: {glob: out.txt, outputEval: '$(self[0] .contents)'}
  # an expression is not evaluated, composed or recognised in part
  arithmetic:
    type: File
    outputBinding: {glob: out.txt, outputEval: '$(self[0].size + 1)'}
  a_method_call:
    type: File
    outputBinding: {glob: out.txt, outputEval: '$(self[0].basename.toUpperCase())'}
  the_length:
    type: File
    outputBinding: {glob: out.txt, outputEval: '$(self.length)'}
  the_second_match:
    type: File
    outputBinding: {glob: out.txt, outputEval: '$(self[1])'}
  a_conditional:
    type: File
    outputBinding: {glob: out.txt, outputEval: '$(self[0] ? self[0] : null)'}
  an_input:
    type: File
    outputBinding: {glob: out.txt, outputEval: '$(inputs.message)'}
  a_function_body:
    type: File
    outputBinding: {glob: out.txt, outputEval: '${return self[0];}'}
  a_function_body_that_trims:
    type: File
    outputBinding: {glob: out.txt, outputEval: '${ return self[0].contents.trim(); }'}
  the_lines:
    type: File
    outputBinding: {glob: out.txt, outputEval: '$(self[0].contents.split(''\n''))'}
  contents_called:
    type: File
    outputBinding: {glob: out.txt, outputEval: '$(self[0].contents())'}
  text_before_it:
    type: File
    outputBinding: {glob: out.txt, outputEval: 'prefix-$(self[0])'}
  written_twice:
    type: File
    outputBinding: {glob: out.txt, outputEval: '$(self[0])$(self[0])'}
  parsed_as_json:
    type: File
    outputBinding: {glob: out.txt, outputEval: '$(JSON.parse(self[0].contents))'}
  parsed_as_a_number:
    type: File
    outputBinding: {glob: out.txt, outputEval: '$(parseInt(self[0].contents))'}
  the_nameroot:
    type: File
    outputBinding: {glob: out.txt, outputEval: '$(self[0].nameroot)'}
  the_nameext:
    type: File
    outputBinding: {glob: out.txt, outputEval: '$(self[0].nameext)'}
  # a field of the file that a Nextflow task cannot give
  the_dirname:
    type: File
    outputBinding: {glob: out.txt, outputEval: '$(self[0].dirname)'}
  the_path:
    type: File
    outputBinding: {glob: out.txt, outputEval: '$(self[0].path)'}
  the_location:
    type: File
    outputBinding: {glob: out.txt, outputEval: '$(self[0].location)'}
  the_checksum:
    type: File
    outputBinding: {glob: out.txt, outputEval: '$(self[0].checksum)'}
  the_secondary_files:
    type: File
    outputBinding: {glob: out.txt, outputEval: '$(self[0].secondaryFiles)'}
  the_format:
    type: File
    outputBinding: {glob: out.txt, outputEval: '$(self[0].format)'}
