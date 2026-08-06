"""Tests for resolver.py: Resolver, ResolvedConnection, ResolvedDatabase, ResolvedProvider, ResolvedModel."""

from __future__ import annotations

import pytest
import typer
from pydantic import ValidationError

from oa_configurator import (
    CDMDatabaseConfig,
    ConnectionConfig,
    GenericDatabaseConfig,
    ModelConfig,
    ProviderConfig,
    Resolver,
    ResolvedCDMDatabase,
    ResolvedConnection,
    ResolvedDatabase,
    ResolvedModel,
    ResolvedProvider,
    ResolvedVectorStore,
    StackConfig,
    VectorStoreConfig,
)
from oa_configurator.resolver import _check_test_collision


class TestResolveConnection:
    def test_sqlite_url(self, minimal_stack):
        r = Resolver(minimal_stack)
        target = r.resolve_connection("db")
        assert isinstance(target, ResolvedConnection)
        assert target.url == "sqlite:///:memory:"
        assert target.safe_url == "sqlite:///:memory:"

    def test_pg_url_contains_password(self, pg_stack):
        r = Resolver(pg_stack)
        target = r.resolve_connection("cdm")
        assert "secret" in target.url
        assert "secret" not in target.safe_url
        assert "***" in target.safe_url

    def test_unknown_connection_raises(self, minimal_stack):
        r = Resolver(minimal_stack)
        with pytest.raises(KeyError, match="Unknown connection"):
            r.resolve_connection("does_not_exist")

    def test_sqlite_path_with_reserved_characters_connects_to_the_right_file(self, tmp_path):
        """`?`/`#` in a sqlite path are query/fragment syntax once rendered
        to a URL string and re-parsed -- create_engine() must not round-trip
        through the string form, or it silently connects to a truncated path."""
        db_path = tmp_path / "emb?x=1#frag.db"
        cfg = StackConfig.for_session(
            connections={"db": ConnectionConfig(dialect="sqlite", database_name=str(db_path))}
        )
        target = Resolver(cfg).resolve_connection("db")
        engine = target.create_engine()
        try:
            with engine.connect():
                pass
        finally:
            engine.dispose()
        assert db_path.exists()

    def test_sqlite_url_string_still_reflects_reserved_characters(self, tmp_path):
        """.url/.safe_url stay plain strings for logging/.env export."""
        db_path = tmp_path / "emb?x=1#frag.db"
        cfg = StackConfig.for_session(
            connections={"db": ConnectionConfig(dialect="sqlite", database_name=str(db_path))}
        )
        target = Resolver(cfg).resolve_connection("db")
        assert str(db_path) in target.url
        assert str(db_path) in target.safe_url


class TestResolveDatabase:
    def test_connection_resolved(self, minimal_stack):
        r = Resolver(minimal_stack)
        res = r.resolve_database("default")
        assert isinstance(res, ResolvedCDMDatabase)
        assert isinstance(res, ResolvedDatabase)
        assert res.connection.name == "db"
        assert res.schema_name == "omop"

    def test_generic_database_has_no_vocab_role(self):
        cfg = StackConfig.for_session(
            connections={"c": ConnectionConfig(dialect="sqlite", database_name=":memory:")},
            databases={"default": GenericDatabaseConfig(connection="c", schema_name="public")},
        )
        r = Resolver(cfg)
        res = r.resolve_database("default")
        assert isinstance(res, ResolvedDatabase)
        assert not isinstance(res, ResolvedCDMDatabase)
        assert res.schema_name == "public"

    def test_vocab_fallback_to_primary(self, minimal_stack):
        r = Resolver(minimal_stack)
        res = r.resolve_database("default")
        assert res.vocab_connection.name == res.connection.name

    def test_vocab_connection_separate(self):
        cfg = StackConfig.for_session(
            connections={
                "cdm": ConnectionConfig(dialect="sqlite", database_name=":memory:"),
                "vocab": ConnectionConfig(dialect="sqlite", database_name=":memory:"),
            },
            databases={
                "default": CDMDatabaseConfig(connection="cdm", vocab_connection="vocab", schema_name="omop"),
            },
        )
        r = Resolver(cfg)
        res = r.resolve_database("default")
        assert res.vocab_connection.name == "vocab"

    def test_vocab_schema_falls_back_to_cdm_schema(self, minimal_stack):
        r = Resolver(minimal_stack)
        res = r.resolve_database("default")
        assert res.vocab_schema == "omop"

    def test_explicit_vocab_schema(self, pg_stack):
        r = Resolver(pg_stack)
        res = r.resolve_database("default")
        assert res.vocab_schema == "omop_vocab"
        assert res.results_schema == "results"

    def test_unknown_database_raises(self, minimal_stack):
        r = Resolver(minimal_stack)
        with pytest.raises(KeyError, match="Unknown database"):
            r.resolve_database("does_not_exist")


class TestResolveProvider:
    def test_resolved(self):
        cfg = StackConfig.for_session(
            providers={"p": ProviderConfig(provider="llamacpp", base_url="http://localhost:8080/v1")},
        )
        r = Resolver(cfg)
        provider = r.resolve_provider("p")
        assert isinstance(provider, ResolvedProvider)
        assert provider.provider == "llamacpp"
        assert provider.base_url == "http://localhost:8080/v1"

    def test_unknown_provider_raises(self):
        cfg = StackConfig.for_session()
        r = Resolver(cfg)
        with pytest.raises(KeyError, match="Unknown provider"):
            r.resolve_provider("does_not_exist")


class TestResolveModel:
    def test_resolved(self):
        cfg = StackConfig.for_session(
            providers={"p": ProviderConfig(provider="llamacpp", base_url="http://localhost:8080/v1")},
            models={"m": ModelConfig(provider="p", model="local-chat", configuration={"max_tokens": 8000})},
        )
        r = Resolver(cfg)
        model = r.resolve_model("m")
        assert isinstance(model, ResolvedModel)
        assert model.provider.provider == "llamacpp"
        assert model.model == "local-chat"
        assert model.configuration == {"max_tokens": 8000}

    def test_resolved_embedding_fields(self):
        cfg = StackConfig.for_session(
            providers={"p": ProviderConfig(provider="ollama")},
            models={
                "m": ModelConfig(
                    provider="p",
                    model="nomic-embed-text",
                    embedding_dim=768,
                    document_prefix="search_document: ",
                    query_prefix="search_query: ",
                )
            },
        )
        r = Resolver(cfg)
        model = r.resolve_model("m")
        assert model.embedding_dim == 768
        assert model.document_prefix == "search_document: "
        assert model.query_prefix == "search_query: "

    def test_resolved_embedding_fields_default_to_none(self):
        cfg = StackConfig.for_session(
            providers={"p": ProviderConfig(provider="ollama")},
            models={"m": ModelConfig(provider="p", model="local-chat")},
        )
        r = Resolver(cfg)
        model = r.resolve_model("m")
        assert model.embedding_dim is None
        assert model.document_prefix is None
        assert model.query_prefix is None

    def test_unknown_model_raises(self):
        cfg = StackConfig.for_session()
        r = Resolver(cfg)
        with pytest.raises(KeyError, match="Unknown model"):
            r.resolve_model("does_not_exist")

    def test_resolved_model_exposes_plain_primitives_for_a_consumer_to_use(self):
        """ResolvedModel is pure data, oa-configurator never imports a consumer package.

        A consumer (e.g. omop-llm) builds whatever it needs from these plain
        fields on its own side, the same way omop_alchemy.config.create_cdm_engine()
        takes a plain ResolvedDatabase and does the omop-alchemy-specific part itself.
        """
        cfg = StackConfig.for_session(
            providers={"p": ProviderConfig(provider="llamacpp", base_url="http://localhost:8080/v1")},
            models={"m": ModelConfig(provider="p", model="local-chat")},
        )
        r = Resolver(cfg)
        model = r.resolve_model("m")
        assert model.provider.provider == "llamacpp"
        assert model.provider.base_url == "http://localhost:8080/v1"
        assert model.model == "local-chat"


class TestResolveVectorStore:
    def test_database_backed(self):
        cfg = StackConfig.for_session(
            connections={"db": ConnectionConfig(dialect="sqlite", database_name=":memory:")},
            databases={"default": GenericDatabaseConfig(connection="db", schema_name="public")},
            vector_stores={"vs": VectorStoreConfig(backend_type="pgvector", database="default")},
        )
        r = Resolver(cfg)
        vs = r.resolve_vector_store("vs")
        assert isinstance(vs, ResolvedVectorStore)
        assert vs.backend_type == "pgvector"
        assert vs.database.name == "default"

    def test_file_backed(self):
        """A sqlite-backed store is a GenericDatabaseConfig whose connection has
        dialect='sqlite'. No separate sqlite_path field, same shape as pgvector."""
        cfg = StackConfig.for_session(
            connections={"f": ConnectionConfig(dialect="sqlite", database_name="/data/emb.db")},
            databases={"emb": GenericDatabaseConfig(connection="f")},
            vector_stores={"vs": VectorStoreConfig(backend_type="sqlitevec", database="emb")},
        )
        r = Resolver(cfg)
        vs = r.resolve_vector_store("vs")
        assert vs.backend_type == "sqlitevec"
        assert vs.database.connection.url == "sqlite:////data/emb.db"

    def test_database_required(self):
        with pytest.raises(ValidationError, match="database"):
            VectorStoreConfig(backend_type="sqlitevec")

    def test_configuration_passthrough(self):
        cfg = StackConfig.for_session(
            connections={"f": ConnectionConfig(dialect="sqlite", database_name=":memory:")},
            databases={"emb": GenericDatabaseConfig(connection="f")},
            vector_stores={
                "vs": VectorStoreConfig(
                    backend_type="sqlitevec",
                    database="emb",
                    configuration={"index_nlist": 128},
                ),
            },
        )
        r = Resolver(cfg)
        vs = r.resolve_vector_store("vs")
        assert vs.configuration == {"index_nlist": 128}

    def test_faiss_cache_dir_passthrough(self):
        cfg = StackConfig.for_session(
            connections={"f": ConnectionConfig(dialect="sqlite", database_name=":memory:")},
            databases={"emb": GenericDatabaseConfig(connection="f")},
            vector_stores={
                "vs": VectorStoreConfig(
                    backend_type="sqlitevec",
                    database="emb",
                    faiss_cache_dir="/data/faiss",
                ),
            },
        )
        r = Resolver(cfg)
        vs = r.resolve_vector_store("vs")
        assert vs.faiss_cache_dir == "/data/faiss"

    def test_faiss_cache_dir_defaults_to_none(self):
        cfg = StackConfig.for_session(
            connections={"f": ConnectionConfig(dialect="sqlite", database_name=":memory:")},
            databases={"emb": GenericDatabaseConfig(connection="f")},
            vector_stores={"vs": VectorStoreConfig(backend_type="sqlitevec", database="emb")},
        )
        r = Resolver(cfg)
        vs = r.resolve_vector_store("vs")
        assert vs.faiss_cache_dir is None

    def test_unknown_vector_store_raises(self):
        cfg = StackConfig.for_session()
        r = Resolver(cfg)
        with pytest.raises(KeyError, match="Unknown vector store"):
            r.resolve_vector_store("does_not_exist")

    def test_dangling_database_reference_rejected_at_construction(self):
        """oa-configurator never imports the owning package's BackendType enum
        (backend_type is a plain string), but a RefTo(GenericDatabaseConfig)
        target still has to actually exist, same as any other domain."""
        with pytest.raises(ValueError, match="unknown database"):
            StackConfig.for_session(
                vector_stores={"vs": VectorStoreConfig(backend_type="pgvector", database="does_not_exist")},
            )

    def test_cdm_database_reference_rejected(self):
        """A vector store's database must be generic-kind. CDM-shaped
        fields (vocab/results roles) mean nothing for an embedding store."""
        cfg_databases = {"cdm_db": CDMDatabaseConfig(connection="c")}
        with pytest.raises(ValueError, match="GenericDatabaseConfig"):
            StackConfig.for_session(
                connections={"c": ConnectionConfig(dialect="sqlite")},
                databases=cfg_databases,
                vector_stores={"vs": VectorStoreConfig(backend_type="pgvector", database="cdm_db")},
            )


class TestSchemaTranslateMap:
    def test_no_results_schema(self, minimal_stack):
        r = Resolver(minimal_stack)
        res = r.resolve_database("default")
        stm = res.schema_translate_map()
        assert stm[None] == "omop"
        assert "results" not in stm

    def test_with_all_schemas(self, pg_stack):
        r = Resolver(pg_stack)
        res = r.resolve_database("default")
        stm = res.schema_translate_map()
        assert stm[None] == "omop"
        assert stm["vocab"] == "omop_vocab"
        assert stm["results"] == "results"

    def test_vocab_schema_defaults_to_cdm(self, minimal_stack):
        r = Resolver(minimal_stack)
        res = r.resolve_database("default")
        stm = res.schema_translate_map()
        assert stm["vocab"] == "omop"


class TestResolveTool:
    def test_tool_extra_dict(self):
        cfg = StackConfig.for_session(
            connections={"db": ConnectionConfig(dialect="sqlite")},
            databases={"default": CDMDatabaseConfig(connection="db", schema_name="omop")},
            tools={"omop_emb": {"backend": "sqlitevec", "path": "/data"}},
        )
        r = Resolver(cfg)
        tool = r.resolve_tool("omop_emb")
        assert tool.extra["backend"] == "sqlitevec"
        assert tool.extra["path"] == "/data"

    def test_unknown_tool_raises(self, minimal_stack):
        r = Resolver(minimal_stack)
        with pytest.raises(KeyError, match="Unknown tool"):
            r.resolve_tool("nonexistent")


class TestCreateEngine:
    def test_sqlite_engine_from_target(self, minimal_stack):
        r = Resolver(minimal_stack)
        target = r.resolve_connection("db")
        engine = target.create_engine()
        assert engine.dialect.name == "sqlite"

    def test_database_create_engine(self, minimal_stack):
        r = Resolver(minimal_stack)
        res = r.resolve_database("default")
        engine = res.create_engine()
        assert engine.dialect.name == "sqlite"


class TestWithOverrides:
    def test_override_connection(self, minimal_stack):
        r = Resolver(minimal_stack)
        r2 = r.with_overrides(
            connections={"db": ConnectionConfig(dialect="sqlite", database_name="/other.db")}
        )
        assert r2.resolve_connection("db").url == "sqlite:////other.db"

    def test_original_unchanged(self, minimal_stack):
        r = Resolver(minimal_stack)
        r.with_overrides(connections={"db": ConnectionConfig(dialect="sqlite", database_name="/other.db")})
        assert r.resolve_connection("db").url == "sqlite:///:memory:"


class TestDiscovery:
    def test_connection_names(self, minimal_stack):
        r = Resolver(minimal_stack)
        assert r.connection_names() == ("db",)

    def test_database_names(self, minimal_stack):
        r = Resolver(minimal_stack)
        assert r.database_names() == ("default",)

    def test_provider_and_model_names(self):
        cfg = StackConfig.for_session(
            providers={"p": ProviderConfig(provider="ollama")},
            models={"m": ModelConfig(provider="p", model="llama3:8b")},
        )
        r = Resolver(cfg)
        assert r.provider_names() == ("p",)
        assert r.model_names() == ("m",)

    def test_vector_store_names(self):
        cfg = StackConfig.for_session(
            connections={"f": ConnectionConfig(dialect="sqlite", database_name=":memory:")},
            databases={"emb": GenericDatabaseConfig(connection="f")},
            vector_stores={"vs": VectorStoreConfig(backend_type="sqlitevec", database="emb")},
        )
        r = Resolver(cfg)
        assert r.vector_store_names() == ("vs",)


class TestCheckTestCollision:
    """_check_test_collision must catch a colliding production connection
    regardless of how it's reachable in the config, since it derives its
    checks from config.connections directly rather than from
    config.databases's own `connection` field."""

    def test_collision_via_primary_connection_raises(self):
        cfg = StackConfig.for_session(
            connections={"prod": ConnectionConfig(dialect="postgresql+psycopg", host="h", port=5432, database_name="d")},
            databases={"cdm_db": CDMDatabaseConfig(connection="prod")},
        )
        new_conn = ConnectionConfig(dialect="postgresql+psycopg", host="h", port=5432, database_name="d", test_only=True)
        with pytest.raises(typer.Exit):
            _check_test_collision(new_conn, cfg)

    def test_collision_via_vocab_connection_only_raises(self):
        """The production connection is reachable only as a CDM database's
        vocab_connection, never as its primary `connection` -- a check
        derived from db.connection alone would miss this."""
        cfg = StackConfig.for_session(
            connections={
                "primary": ConnectionConfig(dialect="postgresql+psycopg", host="h", port=5432, database_name="primary_db"),
                "vocab_prod": ConnectionConfig(dialect="postgresql+psycopg", host="vocab-h", port=5432, database_name="vocab_db"),
            },
            databases={"cdm_db": CDMDatabaseConfig(connection="primary", vocab_connection="vocab_prod")},
        )
        new_conn = ConnectionConfig(dialect="postgresql+psycopg", host="vocab-h", port=5432, database_name="vocab_db", test_only=True)
        with pytest.raises(typer.Exit):
            _check_test_collision(new_conn, cfg)

    def test_collision_via_connection_not_referenced_by_any_database_raises(self):
        """The production connection isn't wired to any [databases.*] entry
        at all -- deriving connections from config.databases would never
        see it."""
        cfg = StackConfig.for_session(
            connections={"orphan_prod": ConnectionConfig(dialect="postgresql+psycopg", host="h", port=5432, database_name="d")},
        )
        new_conn = ConnectionConfig(dialect="postgresql+psycopg", host="h", port=5432, database_name="d", test_only=True)
        with pytest.raises(typer.Exit):
            _check_test_collision(new_conn, cfg)

    def test_no_collision_passes(self):
        cfg = StackConfig.for_session(
            connections={"prod": ConnectionConfig(dialect="postgresql+psycopg", host="h", port=5432, database_name="d")},
            databases={"cdm_db": CDMDatabaseConfig(connection="prod")},
        )
        new_conn = ConnectionConfig(dialect="postgresql+psycopg", host="other-h", port=5432, database_name="d", test_only=True)
        _check_test_collision(new_conn, cfg)  # must not raise
