#!/bin/bash
# Wrapper para chamar a tela ICEQ via python3
# "$@" passa todos os argumentos ( -ng, -ini, etc ) para o script Python

DIR="$(cd "$(dirname "$0")" && pwd)"
python3 "$DIR/iceq_torno.py" "$@"
