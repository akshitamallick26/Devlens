"""Importing this package registers every mapped table on Base.metadata —
required for Alembic autogenerate and for create_all/mock-engine DDL dumps
to see the full schema."""

from app.models.analysis import AnalysisRun
from app.models.artifact import GeneratedArtifact
from app.models.graph import GraphEdge
from app.models.investigation import InvestigationSession, SessionContextRef, SessionMessage
from app.models.rag import CodeChunk, Embedding
from app.models.repository import Repository, RepositoryRevision
from app.models.security import SecurityFinding, SecurityFindingObservation
from app.models.source import SourceBlob, SourceFile
from app.models.symbol import Symbol
from app.models.testing import GeneratedTest, TestExecution

__all__ = [
    "AnalysisRun",
    "CodeChunk",
    "Embedding",
    "GeneratedArtifact",
    "GeneratedTest",
    "GraphEdge",
    "InvestigationSession",
    "Repository",
    "RepositoryRevision",
    "SecurityFinding",
    "SecurityFindingObservation",
    "SessionContextRef",
    "SessionMessage",
    "SourceBlob",
    "SourceFile",
    "Symbol",
    "TestExecution",
]
