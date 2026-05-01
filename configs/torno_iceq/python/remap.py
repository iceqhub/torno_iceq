from stdglue import *
import datetime
import os

TORRE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "torre.txt"
)

def init_stdglue(self):
    self.sticky_params = dict()
    if self.task:
        try:
            with open(TORRE_FILE, "r") as f:
                val = int(f.read().strip())
                if 1 <= val <= 8:
                    self.params["_torre"] = float(val)
                else:
                    self.params["_torre"] = 1.0
        except:
            self.params["_torre"] = 1.0
        print("Torre carregada: posicao %d" % int(self.params["_torre"]))

def salva_torre(self, **words):
    """Chamado via M500 P<pocket> com argspec=p"""
    try:
        print("salva_torre chamado, words=%s task=%d" % (str(words), self.task))
        if 'p' in words:
            pos = int(words['p'])
        else:
            pos = 1
        if 1 <= pos <= 8:
            with open(TORRE_FILE, "w") as f:
                f.write(str(pos))
            print("Torre salva: posicao %d" % pos)
        else:
            print("Torre: posicao invalida %d" % pos)
    except Exception as e:
        print("Erro ao salvar torre: %s" % e)
    return INTERP_OK

def m400(self, *args):
    data = float(datetime.datetime.now().strftime("%m%d%Y%H%M"))
    self.params["_dat"] = data
    return INTERP_OK
