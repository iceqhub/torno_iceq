#!/bin/bash
# iceq_torno.sh — Inicializa a IHM PyQt5 do TORNO ICEQ
# Destino: configs/torno_iceq/iceq_torno.sh
#
# Uso pelo LinuxCNC (DISPLAY = no INI):
#   Não é chamado diretamente como DISPLAY.
#   O LinuxCNC usa DISPLAY = axis e carrega a IHM em paralelo.
#
# Uso manual (desenvolvimento/teste):
#   cd ~/linuxcnc/configs/torno_iceq
#   ./iceq_torno.sh
#
# Para integrar com LinuxCNC como tela principal (substitui Axis),
# altere no INI: DISPLAY = /caminho/para/iceq_torno.sh

set -e

# Diretório do script (garante que imports relativos funcionem)
DIR="$(cd "$(dirname "$0")" && pwd)"

# Exporta INI_FILE_NAME se não estiver definido
# (o LinuxCNC exporta isso automaticamente; aqui é para testes manuais)
if [ -z "$INI_FILE_NAME" ]; then
    export INI_FILE_NAME="$DIR/torno_iceq.ini"
fi

# Garante que o Python encontre os módulos do LinuxCNC
# (normalamente já está no PYTHONPATH pelo ambiente LinuxCNC)
if [ -z "$PYTHONPATH" ]; then
    export PYTHONPATH="/usr/lib/python3/dist-packages:/usr/lib/linuxcnc/python"
fi

# Executa a IHM PyQt5
# Passa todos os argumentos recebidos (ex: -ini, -display, etc.)
exec python3 "$DIR/iceq_torno.py" "$@"
