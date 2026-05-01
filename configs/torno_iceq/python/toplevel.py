import remap
import os

def __init__(self):
    remap.init_stdglue(self)

def __delete__(self):
    if self.task:
        # Salvamento final ao fechar
        try:
            pos = int(self.params["_torre"])
            with open(remap.TORRE_FILE, "w") as f:
                f.write(str(pos))
            print("Torre salva ao fechar: posicao %d" % pos)
        except Exception as e:
            print("Erro ao fechar torre: %s" % e)
