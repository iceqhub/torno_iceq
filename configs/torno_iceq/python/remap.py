from stdglue import *
import datetime
import os

TORRE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "torre.txt"
)

# Atributo Python para rastrear posição da torre
# Separado do self.params para garantir persistência correta

def init_stdglue(self):
    self.sticky_params = dict()
    if self.task:
        try:
            with open(TORRE_FILE, "r") as f:
                val = int(f.read().strip())
                if 1 <= val <= 8:
                    self._torre_pos = val
                else:
                    self._torre_pos = 1
        except:
            self._torre_pos = 1
        # Injeta no params para o NGC ler
        self.params["_torre"] = float(self._torre_pos)
        print("Torre carregada: posicao %d" % self._torre_pos)

def salva_torre(self, **words):
    """Chamado via REMAP M500 pelo .m6.ngc após cada troca"""
    if self.task:
        try:
            # Lê posição atual do params (atualizado pelo NGC)
            pos = int(self.params["_torre"])
            self._torre_pos = pos
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
