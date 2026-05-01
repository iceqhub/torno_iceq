import remap
import os

def __init__(self):
    remap.init_stdglue(self)

def __delete__(self):
    # Salva posição da torre apenas na instância milltask (não no preview)
    if self.task:
        remap.salva_torre(self)
