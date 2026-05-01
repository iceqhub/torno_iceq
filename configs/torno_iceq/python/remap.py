from stdglue import *
import datetime
import os

TORRE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "torre.txt"
)

def le_torre():
    try:
        with open(TORRE_FILE, "r") as f:
            val = int(f.read().strip())
            if 1 <= val <= 8:
                return float(val)
    except:
        pass
    return 1.0

def init_stdglue(self):
    self.sticky_params = dict()
    # Carrega em AMBAS as instâncias (milltask e preview)
    pos = le_torre()
    self.params["_torre"] = pos
    if self.task:
        print("Torre carregada: posicao %d" % int(pos))

def salva_torre(self, **words):
    try:
        if 'p' in words:
            pos = int(words['p'])
            if 1 <= pos <= 8:
                with open(TORRE_FILE, "w") as f:
                    f.write(str(pos))
                print("Torre salva: posicao %d" % pos)
    except Exception as e:
        print("Erro ao salvar torre: %s" % e)
    return INTERP_OK

def m400(self, *args):
    data = float(datetime.datetime.now().strftime("%m%d%Y%H%M"))
    self.params["_dat"] = data
    return INTERP_OK
