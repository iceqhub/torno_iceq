#!/usr/bin/env python3
# torre_monitor.py
# Componente HAL userspace que monitora a ferramenta ativa
# e salva a posição da torre no arquivo torre.txt

import hal
import time
import os
import sys

TORRE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "torre.txt"
)

def pocket_from_tool(tool):
    """Converte número da ferramenta (1-16) para posição da torre (1-8)"""
    if tool <= 0:
        return None
    return int(tool - 8 * int(tool / 9))

def le_torre():
    try:
        with open(TORRE_FILE, "r") as f:
            val = int(f.read().strip())
            if 1 <= val <= 8:
                return val
    except:
        pass
    return 1

def salva_torre(pos):
    try:
        with open(TORRE_FILE, "w") as f:
            f.write(str(pos))
        print("torre_monitor: posicao salva = %d" % pos)
        sys.stdout.flush()
    except Exception as e:
        print("torre_monitor: erro ao salvar: %s" % e)

# Cria componente HAL
h = hal.component("torre_monitor")
h.newpin("tool-number", hal.HAL_S32, hal.HAL_IN)
h.ready()

tool_anterior = -1

print("torre_monitor: iniciado, monitorando ferramenta...")
sys.stdout.flush()

try:
    while True:
        tool_atual = h["tool-number"]
        if tool_atual != tool_anterior and tool_atual > 0:
            pocket = pocket_from_tool(tool_atual)
            if pocket and 1 <= pocket <= 8:
                salva_torre(pocket)
            tool_anterior = tool_atual
        time.sleep(0.5)
except KeyboardInterrupt:
    pass
finally:
    h.exit()
