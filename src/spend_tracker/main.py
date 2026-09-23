from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from threading import RLock
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from spend_tracker.classifiers import MerchantClassifier, UnconfiguredJevClassifier
from spend_tracker.config import Settings
from spend_tracker.db import build_engine, session_factory
from spend_tracker.domain import Category
from spend_tracker.models import ImportRun, MerchantMapping, Transaction
from spend_tracker.parsing import AmountConvention, CsvError, DateFormat, parse_csv
from spend_tracker.schemas import CorrectionIn, ImportOut, MerchantOut, TransactionOut
from spend_tracker.services import correct_category, import_transactions, spending_summary


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.sessions() as session:
        yield session


Db = Annotated[Session, Depends(get_session)]
Limit = Annotated[int, Query(ge=1, le=500)]
Offset = Annotated[int, Query(ge=0)]


def check_date_range(date_from, date_to):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be later than date_to")


def create_app(
    settings: Settings | None = None, classifier: MerchantClassifier | None = None
) -> FastAPI:
    settings = settings or Settings()
    engine = build_engine(settings.database_url.get_secret_value())

    @asynccontextmanager
    async def lifespan(app):
        yield
        engine.dispose()

    app = FastAPI(
        title="Spend Tracker",
        version="0.1.0",
        description="CSV imports, merchant classification, corrections, and spending summaries.",
        lifespan=lifespan,
    )
    app.state.engine = engine
    app.state.sessions = session_factory(engine)
    app.state.classifier = classifier if classifier is not None else UnconfiguredJevClassifier()
    app.state.write_lock = RLock()

    @app.exception_handler(IntegrityError)
    async def conflict_handler(request, exc):
        return JSONResponse(
            status_code=409, content={"detail": "Write conflict; retry the request"}
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_handler(request, exc):
        return JSONResponse(
            status_code=503,
            content={
                "detail": "Database unavailable or schema missing; check connection and migrations"
            },
        )

    @app.get("/health", tags=["system"])
    def health(session: Db):
        for model in (Transaction, MerchantMapping, ImportRun):
            session.execute(select(model.id).limit(1))
        return {
            "status": "ok",
            "database": engine.dialect.name,
            "jev": "unconfigured"
            if isinstance(app.state.classifier, UnconfiguredJevClassifier)
            else "adapter_configured",
        }

    @app.get("/categories", tags=["classification"])
    def categories():
        return list(Category)

    @app.post("/imports", response_model=ImportOut, status_code=201, tags=["imports"])
    def upload_csv(
        session: Db,
        file: Annotated[UploadFile, File()],
        source: Annotated[str, Form(min_length=1, max_length=100)],
        date_format: Annotated[DateFormat, Form()] = "iso",
        amount_convention: Annotated[AmountConvention, Form()] = "expenses_positive",
        currency: Annotated[str | None, Form(pattern=r"^[A-Z]{3}$")] = None,
    ):
        source = source.strip().casefold()
        if not source or len(source) > 100 or "\x00" in source:
            raise HTTPException(422, "source must identify the bank/account in 1–100 characters")
        content = file.file.read(settings.max_upload_bytes + 1)
        if len(content) > settings.max_upload_bytes:
            raise HTTPException(413, "CSV exceeds MAX_UPLOAD_BYTES")
        try:
            parsed = parse_csv(
                content,
                max_rows=settings.max_import_rows,
                date_format=date_format,
                amount_convention=amount_convention,
            )
        except CsvError as exc:
            raise HTTPException(422, str(exc)) from exc
        filename = Path((file.filename or "upload.csv").replace("\\", "/")).name[:255]
        with app.state.write_lock, session.begin():
            run = import_transactions(
                session,
                parsed=parsed,
                filename=filename,
                content=content,
                source=source,
                currency=currency or settings.default_currency,
                classifier=app.state.classifier,
                min_confidence=settings.jev_min_confidence,
            )
        return run

    @app.get("/imports", response_model=list[ImportOut], tags=["imports"])
    def list_imports(session: Db, limit: Limit = 100, offset: Offset = 0):
        return session.scalars(
            select(ImportRun)
            .order_by(ImportRun.created_at.desc(), ImportRun.id)
            .offset(offset)
            .limit(limit)
        ).all()

    @app.get("/imports/{import_id}", response_model=ImportOut, tags=["imports"])
    def get_import(import_id: str, session: Db):
        run = session.get(ImportRun, import_id)
        if run is None:
            raise HTTPException(404, "Import not found")
        return run

    @app.get("/transactions", response_model=list[TransactionOut], tags=["transactions"])
    def list_transactions(
        session: Db,
        category: Category | None = None,
        merchant: str | None = None,
        source: str | None = None,
        import_run_id: str | None = None,
        needs_review: bool | None = None,
        possible_duplicate: bool | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: Limit = 100,
        offset: Offset = 0,
    ):
        check_date_range(date_from, date_to)
        statement = select(Transaction)
        for column, value in (
            (Transaction.category, category),
            (Transaction.normalized_merchant, merchant),
            (Transaction.source, source),
            (Transaction.import_run_id, import_run_id),
            (Transaction.needs_review, needs_review),
            (Transaction.possible_duplicate, possible_duplicate),
        ):
            if value is not None:
                statement = statement.where(column == value)
        if date_from:
            statement = statement.where(Transaction.date >= date_from)
        if date_to:
            statement = statement.where(Transaction.date <= date_to)
        return session.scalars(
            statement.order_by(Transaction.date.desc(), Transaction.id).offset(offset).limit(limit)
        ).all()

    @app.patch("/transactions/{transaction_id}/category", tags=["transactions"])
    def update_category(transaction_id: str, correction: CorrectionIn, session: Db):
        with app.state.write_lock, session.begin():
            try:
                count = correct_category(
                    session, transaction_id, correction.category, correction.scope
                )
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
        return {"updated_transactions": count, "scope": correction.scope}

    @app.get("/merchants", response_model=list[MerchantOut], tags=["classification"])
    def list_merchants(session: Db, limit: Limit = 100, offset: Offset = 0):
        return session.scalars(
            select(MerchantMapping)
            .order_by(MerchantMapping.normalized_merchant)
            .offset(offset)
            .limit(limit)
        ).all()

    @app.get("/analytics/summary", tags=["analytics"])
    def analytics(
        session: Db,
        currency: Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")] = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ):
        check_date_range(date_from, date_to)
        return spending_summary(session, currency or settings.default_currency, date_from, date_to)

    return app


app = create_app()
