# MIT License

from fastapi import FastAPI
from pydantic import BaseModel
from zt411_agent.inference import load_model
from prometheus_client import Counter

app = FastAPI()
model = load_model()
REQUESTS = Counter("requests_total", "Total API requests")


class PredictRequest(BaseModel):
    text: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/predict")
def predict(req: PredictRequest):
    REQUESTS.inc()
    return model.predict(req.text)
