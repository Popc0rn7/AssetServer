# Smoke tests

Files in this directory are explicit integration checks. They require running
services and real local datasets, so they are intentionally excluded from the
default `pytest tests` suite.

After OpenCLIP and Gateway are ready, run the retrieve smoke test with:

```bash
.venv/bin/python tests/smoke/retrieve_gateway.py --download
```

It queries Materials and Articulated through Gateway, then validates the ZIP
asset produced for each source.
