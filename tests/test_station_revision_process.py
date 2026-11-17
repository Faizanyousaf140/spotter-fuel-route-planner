"""A real second process reloads stations while the first retains its index."""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


class CrossProcessRevisionTests(SimpleTestCase):
    def test_running_process_refreshes_after_another_process_loads_stations(self):
        with tempfile.TemporaryDirectory(prefix="spotter-revision-") as folder:
            root = Path(folder)
            env = {**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings", "DJANGO_DEBUG": "True",
                   "SQLITE_PATH": str(root / "stations.sqlite3"), "LOG_LEVEL": "CRITICAL"}
            common = "import django; django.setup()\nfrom django.core.management import call_command\n"
            observer = common + '''
import json,time
from pathlib import Path
from route_api.models import FuelStation
from route_api.services.station_service import get_station_index
call_command('migrate',verbosity=0,interactive=False)
FuelStation.objects.create(opis_id=1,name='OLD',address='I-1',city='Old',state='TX',rack_ids='1',price=3,price_min=3,price_max=3,latitude=35,longitude=-100,geocode_source='test')
before=get_station_index()
root=Path(__import__('os').environ['SQLITE_PATH']).parent
(root/'ready').write_text('ready')
deadline=time.monotonic()+20
while not (root/'reloaded').exists():
    if time.monotonic()>deadline: raise RuntimeError('reload handshake timed out')
    time.sleep(.025)
after=get_station_index()
print(json.dumps({'before':len(before),'after':len(after),'same_index':before is after,'revision_changed':before.revision!=after.revision}),flush=True)
'''
            process = subprocess.Popen([sys.executable, "-B", "-c", observer], env=env,
                                       cwd=settings.BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                deadline = time.monotonic() + 12
                while not (root / "ready").exists():
                    if process.poll() is not None or time.monotonic() > deadline:
                        self.fail("Observer did not initialize its station index")
                    time.sleep(.025)
                writer = subprocess.run([sys.executable, "-B", "-c", common + "call_command('load_stations',verbosity=0)"],
                                        env=env, cwd=settings.BASE_DIR, capture_output=True, timeout=12,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.assertEqual(writer.returncode, 0, writer.stderr.decode())
                (root / "reloaded").write_text("ready")
                stdout, stderr = process.communicate(timeout=8)
                self.assertEqual(process.returncode, 0, stderr.decode())
                result = json.loads(stdout)
                self.assertEqual(result, {"before": 1, "after": 6620, "same_index": False, "revision_changed": True})
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=3)
