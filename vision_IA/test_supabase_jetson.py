import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
from parada_jetson import Supa, ejecutar

db = Supa()
db.login()
pid = os.environ["PARADA_ID"]
ejecutar(db, pid, ("parada", {"id_parada": pid,
                              "personas_esperando": 3,
                              "camion_detectado": False}))
print("OK: revisa registros_parada en Supabase")