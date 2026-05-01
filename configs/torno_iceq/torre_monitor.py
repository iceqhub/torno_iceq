#!/usr/bin/env python3
# torre_monitor.py
# Monitora ferramenta ativa via linuxcnc.stat() e salva em torre.txt

import linuxcnc
import time
import os
import sys

TORRE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "torre.txt"
)

def pocket_from_tool(tool):
    if tool <= 0:
        return None
    return int(tool - 8 * int(tool / 9))

def salva_torre(pos):
    try:
        with open(TORRE_FILE, "w") as f:
            f.write(str(pos))
        print("torre_monitor: posicao salva = %d" % pos)
        sys.stdout.flush()
    except Exception as e:
        print("torre_monitor: erro: %s" % e)

s = linuxcnc.stat()
tool_anterior = -1

print("torre_monitor: iniciado")
sys.stdout.flush()

while True:
    try:
        s.poll()
        tool_atual = s.tool_in_spindle
        if tool_atual != tool_anterior and tool_atual > 0:
            pocket = pocket_from_tool(tool_atual)
            if pocket and 1 <= pocket <= 8:
                salva_torre(pocket)
            tool_anterior = tool_atual
        time.sleep(0.5)
    except Exception as e:
        time.sleep(1)
