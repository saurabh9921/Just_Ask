-- Runs once on first container start, connected to the application DB as its owner.
-- Enables extensions used for observability. Schema objects are created by Alembic.
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
-- The app owner does not need to create objects in other databases' public schemas.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
