-- schema.sql  (SCHEMA_VERSION = 1)

CREATE TABLE IF NOT EXISTS index_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE documents (
  id         INTEGER PRIMARY KEY,
  path       TEXT    NOT NULL UNIQUE,
  size       INTEGER NOT NULL,
  mtime_ns   INTEGER NOT NULL,
  ext        TEXT    NOT NULL CHECK (ext IN ('txt', 'md')),
  title      TEXT    NOT NULL,
  folder     TEXT    NOT NULL,
  stem       TEXT    NOT NULL,
  url        TEXT,
  body       TEXT    NOT NULL,
  indexed_at INTEGER NOT NULL
);

CREATE INDEX documents_ext    ON documents(ext);
CREATE INDEX documents_folder ON documents(folder);

CREATE VIRTUAL TABLE docs_fts USING fts5(
  title,
  stem,
  folder,
  body,
  content      = 'documents',
  content_rowid= 'id',
  tokenize     = 'unicode61 remove_diacritics 2'
);

CREATE TRIGGER documents_ai AFTER INSERT ON documents BEGIN
  INSERT INTO docs_fts(rowid, title, stem, folder, body)
    VALUES (new.id, new.title, new.stem, new.folder, new.body);
END;

CREATE TRIGGER documents_ad AFTER DELETE ON documents BEGIN
  INSERT INTO docs_fts(docs_fts, rowid, title, stem, folder, body)
    VALUES ('delete', old.id, old.title, old.stem, old.folder, old.body);
END;

CREATE TRIGGER documents_au AFTER UPDATE ON documents BEGIN
  INSERT INTO docs_fts(docs_fts, rowid, title, stem, folder, body)
    VALUES ('delete', old.id, old.title, old.stem, old.folder, old.body);
  INSERT INTO docs_fts(rowid, title, stem, folder, body)
    VALUES (new.id, new.title, new.stem, new.folder, new.body);
END;
