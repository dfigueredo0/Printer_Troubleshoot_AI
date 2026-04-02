from src.zt411_agent.models.baseline import BaselineModel


def test_predict():
    model = BaselineModel("sentence-transformers/all-MiniLM-L6-v2")
    result = model.predict("Test printer issue")
    assert "embedding_norm" in result
