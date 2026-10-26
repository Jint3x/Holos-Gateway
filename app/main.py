from fastapi import FastAPI

app = FastAPI(title="Holos Gateway")


@app.get("/")
def hello_world() -> dict[str, str]:
    return {"message": "Hello World"}
