from datetime import datetime
from typing import Optional
from sqlalchemy import create_engine, String, Integer, Float, Text, DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session

from src.config import DB_PATH

# 1. Setup the Database Engine (SQLAlchemy 2.0 style)
engine = create_engine(f"sqlite:///{DB_PATH}", echo=False)

# 2. Define the Base Class
class Base(DeclarativeBase):
    pass

# 3. Define the Document Table
class Document(Base):
    __tablename__ = "documents"

    # Identity
    id: Mapped[int] = mapped_column(primary_key=True)
    file_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    file_path: Mapped[str] = mapped_column(String(500))  # Full local path
    
    # Metadata
    filename: Mapped[str] = mapped_column(String(255))
    language: Mapped[Optional[str]] = mapped_column(String(10), nullable=True) # 'DE' or 'EN'
    
    # AI Evaluation (Populated by Gemini)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    priority_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True) # 1-10
    ai_justification: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    
    # Workflow Status
    status: Mapped[str] = mapped_column(String(20), default="PENDING") 
    # Options: PENDING, PROCESSED, DIGITIZED, IGNORED
    
    last_updated: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    def __repr__(self):
        return f"<Document(filename='{self.filename}', status='{self.status}')>"

# 4. Initialization Function
def init_db():
    """Creates the tables if they don't exist."""
    Base.metadata.create_all(engine)
    print(f"Database initialized at: {DB_PATH}")

if __name__ == "__main__":
    init_db()
