"""Health-only API; importing this module needs no AWS configuration."""

from fastapi import FastAPI


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=3000)


if __name__ == "__main__":
    main()
