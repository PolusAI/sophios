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

def __sophios_output_9f72e(value, int depth, Map copies) {
    if( depth > 0 )
        return value.collect { item -> __sophios_output_9f72e(item, depth - 1, copies) }
    if( value instanceof java.nio.file.Path ) {
        def copied = copies.keySet().find { path -> value.startsWith(path) }
        if( copied == null ) {
            def taken = copies.values().collect { made -> made.name }
            def name = (1..taken.size() + 1).collect { copy -> copy == 1 ? value.name : value.name + '_' + copy }
                .find { candidate -> !(candidate in taken) }
            copies[value] = value.copyTo(workflow.outputDir.resolve(name))
            copied = value
        }
        return ['class': java.nio.file.Files.isDirectory(value) ? 'Directory' : 'File',
                'path': copies[copied].resolve(copied.relativize(value))]
    }
    return value == [] ? null : value
}

process autoseg {
    container 'vjaganat90/ichnaea-segmentation-plugin:devn'
    containerOptions '--nv'
    cpus 4
    memory "64000 MB"
    accelerator 1
    ext cudaVersionMin: '12.6', cudaComputeCapability: '3.0', Directory: ['output']

    input:
    tuple val(__sophios_scatter_index_9f72e), path(output, stageAs: '.sophios_entry_output/*')
    path input
    path model, stageAs: '.sophios_entry_model/*'

    output:
    tuple val(__sophios_scatter_index_9f72e), path("${output.toString().tokenize('/').last()}", glob: false), emit: output

    script:
    """
if [ -e ${__sophios_shell_quote_9f72e((output.toString() - '.sophios_entry_output/'))} ] || [ -L ${__sophios_shell_quote_9f72e((output.toString() - '.sophios_entry_output/'))} ]; then echo 'InitialWorkDirRequirement entry already exists in the task directory:' ${__sophios_shell_quote_9f72e((output.toString() - '.sophios_entry_output/'))} >&2; exit 1; fi
cp -RL -- ${__sophios_shell_quote_9f72e(output.toString())} ${__sophios_shell_quote_9f72e((output.toString() - '.sophios_entry_output/'))}
if [ -e ${__sophios_shell_quote_9f72e('models')} ] || [ -L ${__sophios_shell_quote_9f72e('models')} ]; then echo 'InitialWorkDirRequirement entry already exists in the task directory:' ${__sophios_shell_quote_9f72e('models')} >&2; exit 1; fi
ln -s -- ${__sophios_shell_quote_9f72e(model.toString())} ${__sophios_shell_quote_9f72e('models')}
export HOME="\$PWD"
export SINGULARITY_NO_EVAL=1; __sophios_entrypoint=(); if [ -x /.singularity.d/runscript ]; then __sophios_entrypoint=(/.singularity.d/runscript); fi
\${__sophios_entrypoint[@]+"\${__sophios_entrypoint[@]}"} ${__sophios_shell_quote_9f72e('--inpDir')} ${__sophios_shell_quote_9f72e(input.toString())} ${__sophios_shell_quote_9f72e('--outDir')} ${__sophios_shell_quote_9f72e((output.toString() - '.sophios_entry_output/'))}
    """

    stub:
    """
    echo 'Sophios-generated Nextflow does not support dry runs (-stub-run)' >&2
    exit 1
    """
}

workflow _autoseg {
    take:
    autoseg_output
    autoseg_input
    autoseg_model
    main:
    ch_autoseg_scatter = autoseg_output.flatMap { __d0 ->
        (0..<__d0.size()).collect { i -> tuple(i, __d0[i]) }
    }
    autoseg(ch_autoseg_scatter, autoseg_input, autoseg_model)
    emit:
    segmentation = autoseg.out.output.toSortedList { it[0] }.flatMap { it.collect { row -> row[1] } }
}

workflow {
    main:
    if( !nextflow.NF.isSyntaxParserV2() )
        error 'Sophios-generated Nextflow needs the v2 script parser: unset NXF_SYNTAX_PARSER or set it to v2'
    _autoseg(Channel.value(__sophios_input_9f72e('autoseg_output', params.autoseg_output, 'Directory[]').collect { entry -> file(entry, glob: false) }),
        Channel.value(file(__sophios_input_9f72e('autoseg_input', params.autoseg_input, 'Directory'), glob: false)),
        Channel.value(file(__sophios_input_9f72e('autoseg_model', params.autoseg_model, 'Directory'), glob: false)))
    __sophios_outputs_9f72e = _autoseg.out.segmentation.toList().toList().map { items ->
        def copies = [:]
        def paths = items.flatten().findAll { item -> item instanceof java.nio.file.Path }
        paths.findAll { path -> !paths.any { held -> path.parent.startsWith(held) } }
            .each { path -> __sophios_output_9f72e(path, 0, copies) }
        return [__sophios_output_9f72e(items[0], 1, copies)]
    }

    publish:
    segmentation = __sophios_outputs_9f72e.filter { values -> values[0] != null }.map { values -> values[0] }
}

output {
    segmentation { }
}
