cwlVersion: v1.2
class: CommandLineTool
doc: Every output states one of the two outputEval declarations where it cannot be kept.
baseCommand: echo
inputs:
  message:
    type: string
    inputBinding: {position: 1}
stdout: out.txt
outputs:
  # $(self[0]) is the first match only of a glob that can match one file
  first_of_a_star:
    type: File
    outputBinding: {glob: '*.txt', outputEval: '$(self[0])'}
  first_of_a_question_mark:
    type: File
    outputBinding: {glob: 'out?.txt', outputEval: '$(self[0])'}
  first_of_a_range:
    type: File
    outputBinding: {glob: 'out[0-9].txt', outputEval: '$(self[0])'}
  first_of_an_input_reference:
    type: File
    outputBinding: {glob: '$(inputs.message)', outputEval: '$(self[0])'}
  # loadContents and $(self[0].contents) are one declaration, and $(self[0]) takes no loadContents
  load_contents_alone:
    type: File
    outputBinding: {glob: out.txt, loadContents: true}
  first_with_load_contents:
    type: File
    outputBinding: {glob: out.txt, loadContents: true, outputEval: '$(self[0])'}
  text_without_load_contents:
    type: string
    outputBinding: {glob: out.txt, outputEval: '$(self[0].contents)'}
  text_with_load_contents_false:
    type: string
    outputBinding: {glob: out.txt, loadContents: false, outputEval: '$(self[0].contents)'}
  # the text of a file is a string, of a glob that can match one file
  text_as_an_int:
    type: int
    outputBinding: {glob: out.txt, loadContents: true, outputEval: '$(self[0].contents)'}
  text_as_a_float:
    type: float
    outputBinding: {glob: out.txt, loadContents: true, outputEval: '$(self[0].contents)'}
  text_as_a_file:
    type: File
    outputBinding: {glob: out.txt, loadContents: true, outputEval: '$(self[0].contents)'}
  text_as_strings:
    type: string[]
    outputBinding: {glob: out.txt, loadContents: true, outputEval: '$(self[0].contents)'}
  text_of_a_star:
    type: string
    outputBinding: {glob: '*.txt', loadContents: true, outputEval: '$(self[0].contents)'}
  text_of_a_question_mark:
    type: string
    outputBinding: {glob: 'out?.txt', loadContents: true, outputEval: '$(self[0].contents)'}
  text_of_a_range:
    type: string
    outputBinding: {glob: 'out[0-9].txt', loadContents: true, outputEval: '$(self[0].contents)'}
