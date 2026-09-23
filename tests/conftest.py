import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import make_url

from spend_tracker.config import Settings
from spend_tracker.db import build_engine
from spend_tracker.domain import Category, Prediction
from spend_tracker.main import create_app
from spend_tracker.models import Base


class FakeClassifier:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or Prediction(category=Category.DINING, confidence=0.95)

    def classify(self, merchant, categories):
        self.calls.append(merchant)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture(params=["sqlite"] + (["postgres"] if os.getenv("TEST_DATABASE_URL") else []))
def database_url(request, tmp_path):
    if request.param == "sqlite":
        yield f"sqlite:///{tmp_path}/test.db"
        return
    # Use an isolated schema, never drop tables in the configured database's public schema.
    base_url = os.environ["TEST_DATABASE_URL"]
    engine = build_engine(base_url)
    schema = "test_" + uuid4().hex
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    url = make_url(base_url).update_query_dict({"options": f"-csearch_path={schema}"})
    try:
        yield url.render_as_string(hide_password=False)
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


@pytest.fixture
def app_factory(database_url):
    apps = []

    def factory(classifier=None, **overrides):
        settings = Settings(_env_file=None, database_url=database_url, **overrides)
        app = create_app(settings, classifier)
        Base.metadata.create_all(app.state.engine)
        apps.append(app)
        return app

    yield factory
    for app in apps:
        app.state.engine.dispose()


@pytest.fixture
def client(app_factory):
    with TestClient(app_factory()) as client:
        yield client


def upload(client, csv_text, **data):
    return client.post(
        "/imports",
        files={"file": ("transactions.csv", csv_text.encode(), "text/csv")},
        data={"source": "test-checking", **data},
    )
