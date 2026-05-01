import remap
import os

def __init__(self):
    remap.init_stdglue(self)

def __delete__(self):
    # Salva posição da torre ao fechar o LinuxCNC
    remap.salva_torre(self)
