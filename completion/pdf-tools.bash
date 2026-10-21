# Bash completion for pdf-tools. bash-completion loads it on first use from
# ~/.local/share/bash-completion/completions/pdf-tools.

_pdf_tools() {
    local cur prev words cword
    _init_completion || return

    local commands="compress merge split rotate export protect unprotect arrange update"
    local command="" i
    for ((i = 1; i < cword; i++)); do
        case "${words[i]}" in
            -*) ;;
            *) command="${words[i]}"; break ;;
        esac
    done

    case "$prev" in
        --angle)
            COMPREPLY=($(compgen -W "90 -90 180" -- "$cur"))
            return ;;
        --strength)
            # Lossless, high quality, balanced, strong, maximum.
            COMPREPLY=($(compgen -W "0 25 50 75 100" -- "$cur"))
            return ;;
        --auto)
            COMPREPLY=($(compgen -W "on off" -- "$cur"))
            return ;;
        --output-dir)
            _filedir -d
            return ;;
        -o|--output)
            _filedir pdf
            return ;;
    esac

    if [[ -z "$command" ]]; then
        if [[ "$cur" == -* ]]; then
            COMPREPLY=($(compgen -W "-h --help" -- "$cur"))
        else
            COMPREPLY=($(compgen -W "$commands" -- "$cur"))
        fi
        return
    fi

    if [[ "$cur" == -* ]]; then
        local options="--gui"
        case "$command" in
            compress) options+=" --strength --output-dir --no-verify" ;;
            merge) options+=" -o --output" ;;
            rotate) options+=" --angle" ;;
            update) options="--auto" ;;
        esac
        COMPREPLY=($(compgen -W "$options -h --help" -- "$cur"))
        return
    fi

    case "$command" in
        update) ;;
        merge) _filedir '@(pdf|jpg|jpeg|png|tif|tiff|bmp|gif|webp|jp2)' ;;
        *) _filedir pdf ;;
    esac
}

complete -F _pdf_tools pdf-tools
