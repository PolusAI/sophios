# One input of each type a literal is bound to: text, and a scalar or an array of it, for a literal to pick from.
cwlVersion: v1.2
class: CommandLineTool
baseCommand: 'true'
inputs:
  text_quoted: string
  text_quoted_octal: string
  text_plain: string
  text_plain_octal: string
  flag_or_words: {type: ['null', boolean, {type: array, items: string}]}
  count_or_counts: {type: ['null', int, {type: array, items: int}]}
  word_or_words: {type: ['null', string, {type: array, items: string}]}
  file_or_files: {type: ['null', File, {type: array, items: File}]}
  real_or_counts: {type: ['null', float, {type: array, items: int}]}
  word_or_counts: {type: ['null', string, {type: array, items: int}]}
  word_or_nested_counts: {type: ['null', string, {type: array, items: {type: array, items: int}}]}
  letter_or_letters:
    type:
    - 'null'
    - {type: enum, name: letter, symbols: [a, b]}
    - {type: array, items: {type: enum, name: letters, symbols: [a, b]}}
outputs: {}
