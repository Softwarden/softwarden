"""Larger import. 500 rows keep the suite quick; set IMPORT_SCALE_ROWS=5000 for the timing run."""

import os
import time

from catalog.models import Kind, Resource
from imports import csvio
from tests.test_imports import upload

ROWS = int(os.environ.get("IMPORT_SCALE_ROWS", "500"))


def test_large_import(a, api):
    admin = api(a["users"]["admin"])
    rows = [{"name": f"Scale App {i}", "lifecycle": "active", "criticality": "low"} for i in range(ROWS)]
    data = csvio.write_csv(csvio.TEMPLATE[Kind.APPLICATION], rows)
    t0 = time.perf_counter()
    run = upload(admin, data).json_body
    t1 = time.perf_counter()
    assert run["counts"]["create"] == ROWS
    done = admin.post(f"imports/{run['id']}/commit")
    t2 = time.perf_counter()
    print(f"preview {t1 - t0:.1f}s commit {t2 - t1:.1f}s")
    assert done.status_code == 200 and done.json_body["counts"]["created"] == ROWS
    assert Resource.objects.filter(organization=a["org"], name__startswith="Scale App").count() == ROWS
