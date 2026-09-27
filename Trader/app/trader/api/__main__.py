"""`python -m trader.api`: the API process (supervisord program `api`).

Stub (P4-T1): T3 implements `main()` = `configure_logging("api")` then `uvicorn.run(create_app(), ...)` on
0.0.0.0:8000 with one worker, uvicorn's own logging off (its records reach the root handler), proxy headers
on and a 10 s graceful shutdown.
"""


def main() -> None:
    raise NotImplementedError("P4-T3")


if __name__ == "__main__":
    main()
