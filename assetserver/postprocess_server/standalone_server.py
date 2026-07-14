#!/usr/bin/env python3
"""Start the isolated CPU-only collision postprocess worker."""

import argparse
import os


def main() -> None:
    parser = argparse.ArgumentParser(description="AssetServer postprocess worker")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7100)
    parser.add_argument("--asset-root", default=None)
    parser.add_argument("--staging-root", default=None)
    parser.add_argument("--omp-threads", type=int, default=4)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()
    # CoACD/OpenMP must see this before the application imports coacd.
    os.environ["OMP_NUM_THREADS"] = str(args.omp_threads)

    from assetserver.postprocess_server.server_app import PostprocessServerApp

    import uvicorn

    app = PostprocessServerApp(args.asset_root, args.staging_root)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level.lower())


if __name__ == "__main__":
    main()
