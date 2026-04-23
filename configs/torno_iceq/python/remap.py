from stdglue import *

import datetime

def m400(self, *args):


        data = float(datetime.datetime.now().strftime( "%m%d%Y%H%M"))
        self.params["_dat"] = data
        return INTERP_OK


