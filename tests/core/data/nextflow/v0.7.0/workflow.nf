nextflow.enable.dsl=2

def __sophios_shell_quote_9f72e(value) {
    return "'" + value.toString().replace("'", "'\"'\"'") + "'"
}

def __sophios_input_9f72e(String name, value, String type) {
    def item = type.replace('[]', '').replace('?', '')
    if( type.contains('[]') ) {
        if( value instanceof List )
            return value.collect { element -> __sophios_input_9f72e(name, element, item) }
    }
    else {
        def text = value instanceof String
        def path = value instanceof Map && value.getOrDefault('class', item) == item ? value.path : value
        if( type.endsWith('?') && value == [] && !(item in ['File', 'Directory']) )
            return value
        if( item == 'boolean' && value instanceof Boolean || item == 'int' && value instanceof Integer ||
            item == 'float' && (value instanceof Integer || value instanceof Long ||
                value instanceof Double && Double.isFinite(value)) ||
            item == 'string' && text )
            return value
        if( item == 'boolean' && value in ['true', 'false'] )
            return value == 'true'
        if( item == 'int' && text && value ==~ /[+-]?[0-9]+/ &&
            value.toBigInteger() == value.toBigInteger().intValue() )
            return value.toInteger()
        if( item == 'float' && (text && value ==~ /[+-]?([0-9]+([.][0-9]*)?|[.][0-9]+)([eE][+-]?[0-9]+)?/ ||
            value instanceof BigDecimal) && Double.isFinite(value.toDouble()) )
            return value.toDouble()
        if( item in ['File', 'Directory'] && path instanceof String && path != '' ) {
            if( item == 'File' ? file(path, glob: false).isFile() : file(path, glob: false).isDirectory() )
                return path
            throw new IllegalArgumentException("Sophios workflow input '" + name + "' must be " + type + "; no " +
                item + " exists at '" + path + "'")
        }
    }
    throw new IllegalArgumentException("Sophios workflow input '" + name + "' must be " + type + ", not " +
        value.inspect())
}

process first {

    input:
    val env

    script:
    """
    ${env ? __sophios_shell_quote_9f72e('env') : ''} ${__sophios_shell_quote_9f72e('true')}
    """

    stub:
    """
    echo 'Sophios-generated Nextflow does not support dry runs (-stub-run)' >&2
    exit 1
    """
}

workflow flag_first {
    take:
    first_env
    main:
    first(first_env)
}

workflow {
    if( !nextflow.NF.isSyntaxParserV2() )
        error 'Sophios-generated Nextflow needs the v2 script parser: unset NXF_SYNTAX_PARSER or set it to v2'
    flag_first(Channel.value(__sophios_input_9f72e('first_env', params.first_env, 'boolean')))
}
