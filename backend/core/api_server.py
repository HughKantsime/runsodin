"""The API listener inside the container. Published host ports are configured separately."""
import logging
import os
import sys

CONTAINER_API_PORT = 8000


def main():
    import uvicorn
    from core.config import warn_unused_settings

    # The split API container runs several workers. Warn once before starting them.
    if os.environ.get("SUPERVISOR_PROCESS_NAME") == "backend":
        warn_unused_settings(logging.getLogger("odin.api"))
    uvicorn.main(args=["main:app", "--port", str(CONTAINER_API_PORT), *sys.argv[1:]])


if __name__ == "__main__":
    main()
